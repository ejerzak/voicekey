-- Headless Neovim tests for contrib/nvim; a fake capture command replaces voicekey.
-- Run: nvim --headless -u NONE -i NONE -l tests/nvim-tests.lua
local root = vim.fn.fnamemodify(debug.getinfo(1, "S").source:sub(2), ":p:h:h")
vim.opt.runtimepath:append(root .. "/contrib/nvim")
local runtime_dir = vim.fn.tempname()
vim.fn.mkdir(runtime_dir, "p", tonumber("700", 8))
vim.env.XDG_RUNTIME_DIR = runtime_dir
if vim.v.servername == "" then vim.fn.serverstart() end
vim.cmd.runtime("plugin/voicekey.lua")
local voicekey = require("voicekey")
local api = vim.api

local fake = vim.fn.tempname()
vim.fn.writefile({
  "#!/bin/sh",
  -- The real command prints this line once recording starts; SIGINT finishes, SIGTERM discards.
  -- VK_FAIL: the daemon refuses before recording (stderr, exit 1).
  'if [ -n "$VK_FAIL" ]; then echo "voicekey capture: $VK_FAIL" >&2; exit 1; fi',
  'trap \'printf "%b" "$VK_TEXT"; exit "${VK_EXIT:-0}"\' INT',
  "trap 'exit 130' TERM",
  'echo "Recording; Ctrl-C finishes, SIGTERM cancels." >&2',
  -- VK_PREVIEW: live revisions, one JSON string per line, the last split across writes.
  'if [ -n "$VK_PREVIEW" ]; then printf "%s\\n" "$VK_PREVIEW" >&2; printf "Preview; \\"live wo" >&2; sleep .02; printf "rds\\"\\n" >&2; fi',
  -- VK_STOP_FILE: something else (a global stop) finishes the capture.
  'while :; do if [ -n "$VK_STOP_FILE" ] && [ -e "$VK_STOP_FILE" ]; then',
  -- Split the progress marker across writes, then wait for the test to allow delivery.
  '  printf "Trans" >&2; sleep .02; printf "cribing; microphone stopped.\\n" >&2',
  '  while [ -n "$VK_RELEASE_FILE" ] && [ ! -e "$VK_RELEASE_FILE" ]; do sleep .02; done',
  '  printf "%b" "$VK_TEXT"; exit 0; fi; sleep 0.02; done',
}, fake)
vim.fn.setfperm(fake, "rwx------")
voicekey.setup({ cmd = { fake }, notify = false })

local messages = {}
vim.notify = function(msg) table.insert(messages, msg) end

local function wait(pred, what)
  assert(vim.wait(5000, pred, 10), "timed out waiting for " .. what)
end

local function dictate(text, opts)
  opts = opts or {}
  vim.env.VK_TEXT = text
  vim.env.VK_EXIT = opts.exit
  voicekey.start()
  wait(function() return voicekey.status() == "recording" end, "recording")
  if opts.during then opts.during() end
  if opts.cancel then voicekey.cancel() else voicekey.stop() end
  wait(function() return voicekey.status() == nil end, "completion")
  vim.wait(20)
end

local function buffer(lines, row, col)
  vim.cmd("enew!")
  api.nvim_buf_set_lines(0, 0, -1, false, lines)
  api.nvim_win_set_cursor(0, { row, col })
  return api.nvim_get_current_buf()
end

local function lines() return api.nvim_buf_get_lines(0, 0, -1, false) end

local tests = {}

tests["normal mode inserts after the cursor character with spacing"] = function()
  buffer({ "Kant argued this." }, 1, 11)
  dictate("famously")
  assert(lines()[1] == "Kant argued famously this.", lines()[1])
end

tests["text that starts with punctuation joins the word before it"] = function()
  buffer({ "I wonder" }, 1, 7)
  dictate(", what is it?")
  assert(lines()[1] == "I wonder, what is it?", lines()[1])
end

tests["empty line receives the transcript unchanged"] = function()
  buffer({ "" }, 1, 0)
  dictate("  Hello there.\n")
  assert(lines()[1] == "Hello there.", vim.inspect(lines()))
end

tests["text lands at the start mark after the cursor moves"] = function()
  buffer({ "first line", "second line" }, 1, 9)
  dictate("again", { during = function() api.nvim_win_set_cursor(0, { 2, 3 }) end })
  assert(vim.deep_equal(lines(), { "first line again", "second line" }), vim.inspect(lines()))
end

tests["edits before the mark shift it"] = function()
  buffer({ "one two" }, 1, 6)
  dictate("three", { during = function() api.nvim_buf_set_text(0, 0, 0, 0, 0, { "zero " }) end })
  assert(lines()[1] == "zero one two three", lines()[1])
end

tests["multibyte character under the cursor"] = function()
  buffer({ "café" }, 1, 3)
  dictate("au lait")
  assert(lines()[1] == "café au lait", lines()[1])
end

tests["paragraphs become lines; control characters are removed"] = function()
  buffer({ "" }, 1, 0)
  dictate("First.\\r\\n\\nSecond\\033[31m.\\a")
  assert(vim.deep_equal(lines(), { "First.", "", "Second[31m." }), vim.inspect(lines()))
end

tests["insert mode inserts at the cursor and the cursor follows"] = function()
  buffer({ "Hegel wrote." }, 1, 6)
  -- Headless scripts cannot hold insert mode; report it instead.
  local get_mode = api.nvim_get_mode
  api.nvim_get_mode = function() return { mode = "i", blocking = false } end
  local ok, err = pcall(dictate, "also")
  api.nvim_get_mode = get_mode
  assert(ok, err)
  assert(lines()[1] == "Hegel also wrote.", lines()[1])
  assert(api.nvim_win_get_cursor(0)[2] == 11, vim.inspect(api.nvim_win_get_cursor(0)))
end

tests[":VoiceKey command drives a capture"] = function()
  buffer({ "" }, 1, 0)
  vim.env.VK_TEXT = "by command"
  vim.cmd("VoiceKey")
  wait(function() return voicekey.status() == "recording" end, "recording")
  vim.cmd("VoiceKey stop")
  wait(function() return voicekey.status() == nil end, "completion")
  vim.wait(20)
  assert(lines()[1] == "by command", lines()[1])
  messages = {}
  vim.cmd("VoiceKey setup")
  assert(voicekey.status() == nil and messages[1]:find("unknown action"), vim.inspect(messages))
end

tests["focus records this server and only its own release removes it"] = function()
  local path = runtime_dir .. "/voicekey/nvim-focus"
  vim.cmd.doautocmd("FocusGained")
  assert(vim.fn.readfile(path)[1] == vim.v.servername, vim.inspect(vim.fn.readfile(path)))
  assert(vim.fn.getfperm(path) == "rw-------", vim.fn.getfperm(path))
  assert(vim.fn.getfperm(runtime_dir .. "/voicekey") == "rwx------")
  vim.cmd.doautocmd("FocusLost")
  assert(vim.fn.filereadable(path) == 0, "released on focus loss")
  vim.fn.writefile({ "/run/other-nvim.sock" }, path)
  vim.cmd.doautocmd("FocusLost")
  assert(vim.fn.readfile(path)[1] == "/run/other-nvim.sock", "another instance's claim is kept")
  os.remove(path)
end

local function refused(message)
  vim.env.VK_FAIL = message
  messages = {}
  voicekey.start()
  wait(function() return voicekey.status() == nil end, "refusal")
  vim.wait(20)
  vim.env.VK_FAIL = nil
  return messages[#messages] or ""
end

local function no_marker(buf)
  return #api.nvim_buf_get_extmarks(buf, api.nvim_get_namespaces().voicekey, 0, -1, {}) == 0
end

tests["busy daemon: warning with its reason, nothing inserted, marker cleared"] = function()
  local buf = buffer({ "unchanged" }, 1, 0)
  local msg = refused("Finish the current dictation before starting another")
  assert(msg:find("Finish the current dictation", 1, true), msg)
  assert(lines()[1] == "unchanged" and no_marker(buf), vim.inspect(lines()))
end

tests["outdated daemon: warning says to restart the service"] = function()
  local buf = buffer({ "unchanged" }, 1, 0)
  local msg = refused("Daemon lacks client capture support; restart voicekey.service")
  assert(msg:find("restart voicekey.service", 1, true), msg)
  assert(lines()[1] == "unchanged" and no_marker(buf))
end

tests["absent daemon: warning keeps the error and asks about the service"] = function()
  local buf = buffer({ "unchanged" }, 1, 0)
  local msg = refused("[Errno 2] No such file or directory")
  assert(msg:find("No such file", 1, true) and msg:find("is voicekey.service running?", 1, true), msg)
  assert(lines()[1] == "unchanged" and no_marker(buf))
end

tests["a global stop elsewhere still delivers here"] = function()
  local buf = buffer({ "Hume said" }, 1, 8)
  local stop = vim.fn.tempname()
  local release = vim.fn.tempname()
  vim.env.VK_STOP_FILE = stop
  vim.env.VK_RELEASE_FILE = release
  vim.env.VK_TEXT = "custom is the guide"
  voicekey.start()
  wait(function() return voicekey.status() == "recording" end, "recording")
  vim.fn.writefile({}, stop)
  wait(function() return voicekey.status() == "transcribing" end, "external-stop progress")
  assert(lines()[1] == "Hume said", "no insertion before transcription completes")
  local mark = api.nvim_buf_get_extmarks(buf, api.nvim_get_namespaces().voicekey, 0, -1, { details = true })[1]
  assert(mark[4].virt_text[1][1] == "[voicekey: transcribing]", vim.inspect(mark))
  vim.fn.writefile({}, release)
  wait(function() return voicekey.status() == nil end, "completion")
  vim.wait(20)
  vim.env.VK_STOP_FILE = nil
  vim.env.VK_RELEASE_FILE = nil
  os.remove(stop)
  os.remove(release)
  assert(lines()[1] == "Hume said custom is the guide", lines()[1])
  assert(no_marker(buf))
end

tests["cancel inserts nothing"] = function()
  buffer({ "unchanged" }, 1, 0)
  dictate("discarded", { cancel = true })
  assert(lines()[1] == "unchanged", lines()[1])
end

tests["failed capture inserts nothing and warns"] = function()
  buffer({ "unchanged" }, 1, 0)
  messages = {}
  dictate("partial", { exit = 1 })
  assert(lines()[1] == "unchanged", lines()[1])
  assert(messages[#messages]:find("exit 1"), vim.inspect(messages))
end

tests["no marker remains after delivery"] = function()
  local buf = buffer({ "x" }, 1, 0)
  dictate("y")
  local ns = api.nvim_get_namespaces().voicekey
  assert(#api.nvim_buf_get_extmarks(buf, ns, 0, -1, {}) == 0)
end

tests["deleted buffer is reported, not an error"] = function()
  local buf = buffer({ "gone" }, 1, 0)
  messages = {}
  dictate("lost", { during = function()
    vim.cmd("enew!")
    api.nvim_buf_delete(buf, { force = true })
  end })
  assert(messages[#messages]:find("buffer closed"), vim.inspect(messages))
  assert(messages[#messages]:find(vim.fn.expand("~/.local/share/voicekey/venv/bin/python"), 1, true),
    "recovery uses the installed interpreter")
  assert(messages[#messages]:find(" -m voicekey --last", 1, true), "recovery goes through the daemon journal")
end

local function marker_text()
  local marks = api.nvim_buf_get_extmarks(0, -1, 0, -1, { details = true })
  local chunks = marks[1] and marks[1][4].virt_text or {}
  return table.concat(vim.tbl_map(function(chunk) return chunk[1] end, chunks))
end

tests["live preview shows as virtual text and the final transcript replaces it"] = function()
  buffer({ "Kant argued this." }, 1, 11)
  vim.env.VK_PREVIEW = 'Preview; "first\\nguess"'
  local seen = {}
  local ok, err = pcall(dictate, "famously", { during = function()
    wait(function()
      local text = marker_text()
      if seen[#seen] ~= text then table.insert(seen, text) end
      return text == "live words "
    end, "latest preview")
  end })
  vim.env.VK_PREVIEW = nil
  assert(ok, err)
  assert(vim.tbl_contains(seen, "first ↵ guess "), "multi-line preview is shown on one line: " .. vim.inspect(seen))
  assert(lines()[1] == "Kant argued famously this.", lines()[1])
  assert(#api.nvim_buf_get_extmarks(0, -1, 0, -1, {}) == 0, "preview removed")
end

tests["preview never edits the buffer and cancel leaves nothing"] = function()
  buffer({ "unchanged" }, 1, 0)
  vim.env.VK_PREVIEW = 'Preview; "draft"'
  local ok, err = pcall(dictate, "ignored", { cancel = true, during = function()
    -- Spaced like the final text: after "u", before "n".
    wait(function() return marker_text() == " live words " end, "preview")
    assert(vim.deep_equal(lines(), { "unchanged" }), vim.inspect(lines()))
  end })
  vim.env.VK_PREVIEW = nil
  assert(ok, err)
  assert(vim.deep_equal(lines(), { "unchanged" }), vim.inspect(lines()))
  assert(#api.nvim_buf_get_extmarks(0, -1, 0, -1, {}) == 0)
end

tests["preview can be turned off"] = function()
  voicekey.setup({ preview = false })
  buffer({ "" }, 1, 0)
  vim.env.VK_PREVIEW = 'Preview; "draft"'
  local ok, err = pcall(dictate, "done", { during = function()
    vim.wait(100)
    assert(marker_text() == "[voicekey: recording]", marker_text())
  end })
  vim.env.VK_PREVIEW = nil
  voicekey.setup({ preview = true })
  assert(ok, err)
  assert(lines()[1] == "done", lines()[1])
end

tests["daemon pins and :VoiceKey share one draft renderer"] = function()
  local function rpc(method, args)
    return vim.json.decode(voicekey.rpc(vim.json.encode({
      method = method, args = args, expires = os.time() + 60 })))
  end
  buffer({ "Kant argued this." }, 1, 11)
  vim.g.voicekey_focused = true
  voicekey.setup({ preview = false })  -- governs :VoiceKey drafts only
  local ok, err = pcall(function()
    assert(rpc("pin", { id = "p" }).status == "ok")
    assert(marker_text() == "[voicekey: listening]", marker_text())
    assert(rpc("preview", { id = "p", text = "first\nguess" }).status == "ok")
    assert(marker_text() == "first ↵ guess ", marker_text())
    assert(rpc("preview", { id = "p", text = "" }).status == "ok")
    assert(marker_text() == "", "empty preview clears draft and label: " .. marker_text())
    -- While a pin is open, a :VoiceKey capture keeps its own mark and state.
    buffer({ "" }, 1, 0)
    voicekey.start()
    wait(function() return voicekey.status() == "recording" end, "recording")
    assert(marker_text() == "[voicekey: recording]", marker_text())
    voicekey.cancel()
    wait(function() return voicekey.status() == nil end, "cancel")
  end)
  rpc("unpin", { id = "p" })
  vim.g.voicekey_focused = nil
  voicekey.setup({ preview = true })
  assert(ok, err)
end

tests["unmodifiable buffer is refused before recording"] = function()
  buffer({ "read only" }, 1, 0)
  vim.bo.modifiable = false
  voicekey.start()
  assert(voicekey.status() == nil)
  vim.bo.modifiable = true
end

local failed = 0
local names = vim.tbl_keys(tests)
table.sort(names)
for _, name in ipairs(names) do
  local ok, err = pcall(tests[name])
  if voicekey.status() then voicekey.cancel(); vim.wait(2000, function() return voicekey.status() == nil end) end
  print((ok and "ok    " or "FAIL  ") .. name .. (ok and "" or ("\n      " .. tostring(err))))
  if not ok then failed = failed + 1 end
end
os.remove(fake)
vim.fn.delete(runtime_dir, "rf")
print(("%d/%d passed"):format(#names - failed, #names))
os.exit(failed == 0 and 0 or 1)
