-- voicekey.lua — dictate into the current Neovim buffer.
--
-- Runs `python -m voicekey --capture-to-stdout` and inserts its transcript as
-- buffer text with nvim_buf_set_text. Nothing is typed, so the editor mode
-- cannot turn dictated words into commands, and no evdev access or input
-- method is involved. The command is a thin client of the running voicekey
-- daemon: it shares the daemon's loaded models and configuration, and
-- transcripts go to the daemon's journal (`python -m voicekey --last`). Requires
-- Neovim 0.10 and a running daemon with client capture support.
if vim.fn.has("nvim-0.10") == 0 then
  error("voicekey.lua requires Neovim 0.10 or newer")
end

local M = {}

local api = vim.api
local ns = api.nvim_create_namespace("voicekey")
local voicekey_python = vim.fn.expand("~/.local/share/voicekey/venv/bin/python")

local config = {
  -- Client of the running daemon: prints "Recording;" on stderr once the
  -- microphone is live, "Transcribing;" when it stops, and with --preview
  -- `Preview; "<json string>"` for each live revision. It finishes on SIGINT,
  -- cancels on SIGTERM, and writes the final transcript to stdout. The
  -- daemon's configuration applies.
  cmd = { voicekey_python, "-m", "voicekey", "--capture-to-stdout", "--client-name", "Neovim", "--preview" },
  -- Show the recording state as inline virtual text at the insertion point.
  marker = true,
  -- Show the live transcript there as you speak (virtual text, not buffer
  -- text); the final transcript replaces it.
  preview = true,
  -- Add a space between the transcript and adjacent words.
  spacing = true,
  notify = true,
}

local function recovery_hint()
  local python = config.cmd[2] == "-m" and config.cmd[3] == "voicekey" and config.cmd[1] or voicekey_python
  return "recover it with `" .. vim.fn.shellescape(python)
    .. " -m voicekey --last` or the same command with `--copy-last`"
end

-- One capture at a time: { proc, buf, mark, state }.
local active

local labels = {
  loading = "[voicekey: loading]",
  recording = "[voicekey: recording]",
  transcribing = "[voicekey: transcribing]",
}

local function notify(msg, level)
  if config.notify or (level or vim.log.levels.INFO) >= vim.log.levels.WARN then
    vim.notify("voicekey: " .. msg, level)
  end
end

local function define_highlights()
  api.nvim_set_hl(0, "VoiceKeyPreview", { link = "Comment", default = true })
end
define_highlights()

local spaced

-- Drafts occupy virtual lines below their anchor, wrapping without buffer edits.
local function draft_lines(buf, text)
  local wins = vim.fn.win_findbuf(buf)
  local width = math.max(20, (#wins > 0 and api.nvim_win_get_width(wins[1]) or 80) - 4)
  local lines = {}
  for _, line in ipairs(vim.split(text, "\n", { plain = true })) do
    local part, columns = "", 0
    for word in line:gmatch("%s*%S+") do
      if columns + vim.fn.strdisplaywidth(word, columns) > width and part ~= "" then
        table.insert(lines, { { part, "VoiceKeyPreview" } })
        part, columns = "", 0
        word = word:gsub("^%s+", "")
      end
      -- Only a word longer than a whole row needs character-level wrapping.
      for _, char in ipairs(vim.fn.split(word, "\\zs")) do
        local size = vim.fn.strdisplaywidth(char, columns)
        if columns + size > width and part ~= "" then
          table.insert(lines, { { part, "VoiceKeyPreview" } })
          part, columns = "", 0
          size = vim.fn.strdisplaywidth(char)
        end
        part, columns = part .. char, columns + size
      end
    end
    table.insert(lines, { { part, "VoiceKeyPreview" } })
  end
  return lines
end

-- The one renderer for :VoiceKey captures and daemon pins. `preview` is only
-- ever draft transcript text; `label` is a pin's status (a capture's status
-- comes from its state).
local function place(capture, row, col)
  local opts = { id = capture.mark, right_gravity = true }
  if capture.draft then
    opts.virt_text = { { " [voicekey: draft]", "Comment" } }
    opts.virt_text_pos = "inline"
    opts.virt_lines = draft_lines(capture.buf, capture.preview or "")
    capture.mark = api.nvim_buf_set_extmark(capture.buf, ns, row, col, opts)
    return
  end
  local text, hl = capture.preview, "VoiceKeyPreview"
  if text then
    -- A draft of the text to come, spaced like the final insertion; line
    -- breaks are shown as marks because the virtual text stays on one line.
    text = spaced(capture.buf, row, col, (text:gsub("%s*\n%s*", " ↵ ")))
    if capture.state == "transcribing" then
      text = text .. " …"
    end
  else
    text, hl = capture.label or (config.marker and labels[capture.state]), "Comment"
  end
  if text then
    opts.virt_text = { { text, hl } }
    opts.virt_text_pos = "inline"
  end
  capture.mark = api.nvim_buf_set_extmark(capture.buf, ns, row, col, opts)
end

local function redraw(capture)
  if not api.nvim_buf_is_valid(capture.buf) then
    return
  end
  local pos = api.nvim_buf_get_extmark_by_id(capture.buf, ns, capture.mark, {})
  if #pos > 0 then
    place(capture, pos[1], pos[2])
  end
end

local function set_state(capture, state)
  capture.state = state
  redraw(capture)
  vim.cmd.redrawstatus()
end

-- An empty draft clears the draft and any label: the daemon's pin clear.
local function set_preview(capture, text)
  if type(text) ~= "string" then
    return
  end
  text = vim.trim((text:gsub("[%z\1-\8\11-\31\127]", "")))
  if text == "" then
    capture.preview, capture.label = nil, nil
  elseif text ~= capture.preview then
    capture.preview = text
  else
    return
  end
  redraw(capture)
end

-- One stderr line from the capture command, on the main loop.
local function progress(capture, line)
  if active ~= capture then
    return
  end
  if line:find("^Preview; ") then
    local ok, text = pcall(vim.json.decode, line:sub(10))
    if ok and config.preview then
      set_preview(capture, text)
    end
  elseif line:find("Transcribing;", 1, true) then
    if capture.state ~= "transcribing" then
      set_state(capture, "transcribing")
    end
  elseif line:find("Recording;", 1, true) and capture.state == "loading" then
    set_state(capture, "recording")
  end
end

-- Normal mode inserts after the cursor character, like `a`; insert mode at the cursor.
local function insertion_point()
  local row, col = unpack(api.nvim_win_get_cursor(0))
  local mode = api.nvim_get_mode().mode
  if not mode:find("^i") and not mode:find("^R") then
    local line = api.nvim_get_current_line()
    local char = vim.fn.matchstr(line:sub(col + 1), "^.")
    col = col + #char
  end
  return row - 1, col
end

-- Transcript as buffer lines: CR and other control characters removed, outer space trimmed.
local function prepare(text)
  text = text:gsub("\r\n?", "\n"):gsub("[%z\1-\8\11-\31\127]", ""):gsub("^%s+", ""):gsub("%s+$", "")
  return text
end

function spaced(buf, row, col, text)
  if not config.spacing then
    return text
  end
  local line = api.nvim_buf_get_lines(buf, row, row + 1, false)[1] or ""
  local before, after = line:sub(col, col), line:sub(col + 1, col + 1)
  if before ~= "" and not before:match("[%s%(%[{\"'`]") and not text:match("^[,.;:!?%)%]}]") then
    text = " " .. text
  end
  if after ~= "" and after:match("[%w]") then
    text = text .. " "
  end
  return text
end

-- Shared by client capture and daemon pins; never selects a window/buffer.
local function position(capture)
  if not capture or not api.nvim_buf_is_valid(capture.buf) then
    return nil, "buffer closed"
  end
  if not api.nvim_buf_is_loaded(capture.buf) then return nil, "buffer unloaded" end
  if not vim.bo[capture.buf].modifiable then return nil, "buffer is not modifiable" end
  local pos = api.nvim_buf_get_extmark_by_id(capture.buf, ns, capture.mark, {})
  if #pos == 0 then return nil, "insertion point lost" end
  return pos
end

local function put(capture, text, keep_pin)
  local pos, reason = position(capture)
  if not pos then return false, reason end
  local buf = capture.buf
  local row, col = pos[1], pos[2]
  text = spaced(buf, row, col, text)
  local lines = vim.split(text, "\n", { plain = true })
  -- Keep a cursor that sits exactly at the insertion point after the new text.
  local follow = {}
  for _, win in ipairs(vim.fn.win_findbuf(buf)) do
    local cursor = api.nvim_win_get_cursor(win)
    if cursor[1] - 1 == row and cursor[2] == col then
      table.insert(follow, win)
    end
  end
  local ok, err = pcall(api.nvim_buf_set_text, buf, row, col, row, col, lines)
  if not ok then
    return false, "insert failed (" .. err .. ")", true
  end
  local end_row = row + #lines - 1
  local end_col = (#lines == 1 and col or 0) + #lines[#lines]
  if keep_pin then
    capture.draft = nil
    capture.preview, capture.label = nil, nil
    place(capture, end_row, end_col)
  else
    api.nvim_buf_del_extmark(buf, ns, capture.mark)
  end
  for _, win in ipairs(follow) do
    -- Cursor housekeeping must not turn a confirmed buffer edit into a retry.
    pcall(api.nvim_win_set_cursor, win, { end_row + 1, end_col })
  end
  return true
end

local function remove(capture)
  if capture and api.nvim_buf_is_valid(capture.buf) then
    pcall(api.nvim_buf_del_extmark, capture.buf, ns, capture.mark)
  end
end

local function deliver(capture, result)
  if result.signal ~= 0 and result.code == 0 then result.code = 128 + result.signal end
  if capture.cancelled then remove(capture); notify("cancelled"); return end
  local text = prepare(result.stdout or "")
  if result.code ~= 0 or text == "" then
    remove(capture)
    local reason = result.code ~= 0 and ("exit " .. result.code) or "no speech"
    local detail = vim.trim(capture.stderr:match("[^\n]*voicekey capture:[^\n]*") or "")
    if detail:find("No such file", 1, true) or detail:find("Connection refused", 1, true) then
      detail = detail .. " (is voicekey.service running?)"
    end
    notify("no text (" .. reason .. ")" .. (detail ~= "" and ": " .. detail or ""), vim.log.levels.WARN)
    return
  end
  local ok, reason = put(capture, text, false)
  if not ok then
    remove(capture)
    notify(reason .. "; " .. recovery_hint(), vim.log.levels.WARN)
  end
end

-- Daemon protocol. Requests carry a wall-clock expiry and insertions additionally
-- carry the journal's revocable permission file and unique operation ID.
local pins, operations = {}, {}
local function now()
  local seconds, micros = vim.uv.gettimeofday()
  return seconds + micros / 1000000
end
local function refused(reason) return { status = "refused", reason = reason } end

-- Dictation behaves as typing: from normal mode, enter insert mode as `a` does.
-- The switch takes effect when this request returns to the main loop.
local function enter_insert(pin)
  local mode = api.nvim_get_mode().mode
  if mode:find("^i") or mode:find("^ni") then
    -- A new binding in the same buffer shares the insert session we entered.
    -- Insert mode chosen by the user has no owner and must remain theirs.
    for _, other in pairs(pins) do
      if other ~= pin and other.buf == pin.buf and other.restore_normal then
        pin.restore_normal = true
        break
      end
    end
    return
  end
  if mode ~= "n" then return end
  local row, col = insertion_point()
  if col >= #api.nvim_get_current_line() then
    vim.cmd("startinsert!")
  else
    api.nvim_win_set_cursor(0, { row + 1, col })
    vim.cmd("startinsert")
  end
  pin.restore_normal = true
end

-- Return to normal mode (like <Esc>) if this pin owns the insert session and the
-- user has not left it since (see the ModeChanged autocmd), its buffer is
-- still the current one, and no other live pin also relies on it.
local function leave_insert(pin)
  if not (pin and pin.restore_normal) then return end
  pin.restore_normal = nil
  for _, other in pairs(pins) do
    if other ~= pin and other.buf == pin.buf and other.restore_normal then return end
  end
  if api.nvim_get_current_buf() == pin.buf and api.nvim_get_mode().mode:find("^i") then
    vim.cmd("stopinsert")
  end
end

local function dispatch(request)
  local method, args = request.method, request.args
  if type(request.expires) ~= "number" or now() >= request.expires then
    return refused("Neovim request expired")
  end
  -- Bound caches: abandoned pins expire after a day; operation results are kept
  -- through their execution deadline, after which the request itself is refused.
  for id, pin in pairs(pins) do
    if now() - pin.used > 86400 then remove(pin); pins[id] = nil end
  end
  for id, op in pairs(operations) do if now() > op.expires then operations[id] = nil end end
  if method == "status" then
    return { status = "ok", pid = vim.fn.getpid(), server = vim.v.servername,
      focused = vim.g.voicekey_focused == true }
  elseif method == "pin" then
    if vim.g.voicekey_focused ~= true then return refused("Neovim is not focused") end
    if active then return refused("Neovim client capture is active") end
    local buf = api.nvim_get_current_buf()
    if not vim.bo[buf].modifiable or vim.bo[buf].buftype ~= "" then
      return refused("buffer is not an editable text buffer")
    end
    local mode = api.nvim_get_mode().mode
    if mode:find("^no") or mode:find("^[vV]") or mode:byte() == 22 then
      return refused("selection or operator pending")
    end
    if not pins[args.id] then
      local pin = { buf = buf, used = now(), label = "[voicekey: listening]" }
      place(pin, insertion_point())
      pins[args.id] = pin
      enter_insert(pin)
    end
    local pin = pins[args.id]
    local pos, reason = position(pin)
    if not pos then return refused(reason) end
    local line = api.nvim_buf_get_lines(buf, pos[1], pos[1] + 1, false)[1]
    return { status = "ok", before = line:sub(1, pos[2]), buffer = api.nvim_buf_get_name(buf) }
  elseif method == "unpin" then
    leave_insert(pins[args.id])
    remove(pins[args.id]); pins[args.id] = nil
    return { status = "ok" }
  end
  if method == "insert" and operations[args.operation] then return operations[args.operation].reply end
  local pin = pins[args.id]
  local pos, reason = position(pin)
  if not pos then return refused(reason) end
  pin.used = now()
  if method == "check" then return { status = "ok" } end
  if method == "draft" then
    if vim.bo[pin.buf].buftype ~= "" then return refused("drafts require an editable text buffer") end
    if not pin.draft then
      for id, old in pairs(pins) do
        if id ~= args.id and old.draft then remove(old); pins[id] = nil end
      end
    end
    pin.draft = true
    set_preview(pin, args.text or "")
    return { status = "ok" }
  end
  if method == "preview" then
    set_preview(pin, args.text or "")
    return { status = "ok" }
  elseif method == "insert" then
    if args.permit and args.permit ~= vim.NIL and vim.fn.filereadable(args.permit) ~= 1 then return refused("insertion cancelled") end
    local text = prepare(args.text)
    if now() >= request.expires then return refused("insertion expired") end
    local ran, ok, why, uncertain = pcall(put, pin, text, args.keep_pin)
    if not ran then why, ok, uncertain = tostring(ok), false, true end
    local reply = ok and { status = "ok" } or { status = uncertain and "unknown" or "refused", reason = why }
    operations[args.operation] = { reply = reply, expires = request.expires }
    if not args.keep_pin then leave_insert(pin); remove(pin); pins[args.id] = nil end
    return reply
  end
  return refused("unknown Neovim method")
end

function M.rpc(payload)
  local ok, reply = pcall(function() return dispatch(vim.json.decode(payload)) end)
  return vim.json.encode(ok and reply or { status = "unknown", reason = tostring(reply) })
end

--- Start recording; the transcript lands at the current cursor position.
function M.start()
  if active then
    return
  end
  if vim.fn.executable(config.cmd[1]) ~= 1 then
    notify("not executable: " .. config.cmd[1], vim.log.levels.ERROR)
    return
  end
  local buf = api.nvim_get_current_buf()
  if not vim.bo[buf].modifiable then
    notify("buffer is not modifiable", vim.log.levels.WARN)
    return
  end
  -- stderr keeps non-preview lines for failure warnings; partial holds an unfinished line.
  local capture = { buf = buf, state = "loading", stderr = "", partial = "" }
  place(capture, insertion_point())
  active = capture
  local ok, proc = pcall(vim.system, config.cmd, {
    text = true,
    -- A stderr callback means vim.system does not collect stderr itself.
    -- Handle whole lines, since markers can be split across reads; keep all
    -- but previews for failure warnings.
    stderr = function(_, data)
      local chunk = capture.partial .. (data or "\n")
      local lines = vim.split(chunk, "\n", { plain = true })
      capture.partial = table.remove(lines)
      for _, line in ipairs(lines) do
        if not line:find("^Preview; ") then
          capture.stderr = capture.stderr .. line .. "\n"
        end
      end
      if #lines > 0 then
        vim.schedule(function()
          for _, line in ipairs(lines) do
            progress(capture, line)
          end
        end)
      end
    end,
  }, function(result)
    vim.schedule(function()
      if active == capture then
        active = nil
      end
      deliver(capture, result)
      vim.cmd.redrawstatus()
    end)
  end)
  if not ok then
    active = nil
    api.nvim_buf_del_extmark(buf, ns, capture.mark)
    notify("could not start: " .. proc, vim.log.levels.ERROR)
    return
  end
  capture.proc = proc
  vim.cmd.redrawstatus()
end

--- Finish recording and insert the transcript (SIGINT).
function M.stop()
  if active and active.state ~= "transcribing" then
    set_state(active, "transcribing")
    active.proc:kill("sigint")
  end
end

--- Discard the recording (SIGTERM); nothing is inserted.
function M.cancel()
  if active then
    active.cancelled = true
    active.proc:kill("sigterm")
  end
end

function M.toggle()
  if active then
    M.stop()
  else
    M.start()
  end
end

--- "loading", "recording", "transcribing", or nil; for statuslines.
function M.status()
  return active and active.state or nil
end

function M.setup(opts)
  config = vim.tbl_deep_extend("force", config, opts or {})
  if opts and opts.cmd then
    config.cmd = opts.cmd
  end
end

local group = api.nvim_create_augroup("voicekey", { clear = true })
api.nvim_create_autocmd("ColorScheme", { group = group, callback = define_highlights })

-- Leaving the insert mode that dictation entered hands the mode back to the
-- user: a later session end must not override a mode they chose since.
-- Completion menus and i_CTRL-O stay within that insert session.
api.nvim_create_autocmd("ModeChanged", {
  group = group,
  pattern = "i*:*",
  callback = function()
    local new = vim.v.event.new_mode
    if new:find("^i") or new:find("^ni") then return end
    for _, pin in pairs(pins) do pin.restore_normal = nil end
  end,
})

-- Never leave the microphone recording after Neovim exits.
api.nvim_create_autocmd("VimLeavePre", {
  group = group,
  callback = function()
    if active then
      active.cancelled = true
      active.proc:kill("sigterm")
    end
  end,
})

return M
