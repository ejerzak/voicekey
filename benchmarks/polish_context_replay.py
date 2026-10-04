"""Replay persistent-mode cleanup requests that carried context.

Reads the recovery journal's final events that recorded ``polish_context``,
sends each request again to the configured cleanup model on a private port
(``diagnostic_polisher``: the running service's server is not touched), and
reports what the live run did against what the current code makes of the
reply. Nothing is typed, copied or sent off the machine. Replies are written
to OUT for inspection; they are your dictation, so keep them out of the repo
(tests/data holds synthetic requests of the same shapes).

    ~/.local/share/voicekey/venv/bin/python benchmarks/polish_context_replay.py [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from voicekey import config, polish, recovery  # noqa: E402
from voicekey.polish import PolishError, current_reply, judge  # noqa: E402


def requests(directory: str):
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "final" and event.get("polish_context"):
                    yield event


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="/tmp/voicekey-polish-context-replay.json")
    parser.add_argument("--sessions", default=os.path.join(recovery.STATE_DIR, "sessions"))
    args = parser.parse_args()
    cfg = config.load()
    events = sorted(requests(args.sessions), key=lambda event: event["time"])
    if not events:
        print("no cleanup requests with context in", args.sessions)
        return 1
    results, tally = [], Counter()
    with polish.diagnostic_polisher(cfg.polish) as model:
        if model is None:
            print("cleanup is disabled ([polish] backend = \"none\")")
            return 1
        for event in events:
            context, raw = polish.context_tail(event["polish_context"]), event["raw"]
            system, user = model.format.messages(context + " " + raw, event.get("polish_style"))
            reply = model.backend.chat(system, user, polish.max_tokens_for(context + " " + raw), 30)
            try:
                new, _ = current_reply(reply, context, raw)
                verdict = judge(raw, new) or "applied"
                new = new.text
            except PolishError as exc:
                verdict, new = str(exc), None
            live = event.get("polish_result", "")
            tally[(live == "applied", verdict == "applied")] += 1
            results.append({"context": context, "raw": raw, "reply": reply.text.strip(),
                            "new": new, "verdict": verdict, "live": live})
            mark = "ok " if verdict == "applied" else "-- "
            print(mark + (new if new is not None else f"[{verdict}] {raw}")[:120])
    with open(args.out, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(results, handle, indent=1, ensure_ascii=False)
    print(f"\n{len(results)} requests; live applied {tally[True, True] + tally[True, False]}, "
          f"now applied {tally[True, True] + tally[False, True]} "
          f"(newly applied {tally[False, True]}, newly refused {tally[True, False]}); replies in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
