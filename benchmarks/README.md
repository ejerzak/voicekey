# Speech-model benchmarks

These scripts exercise ASR only (except the cleanup replay below): no
microphone, keyboard, compositor, Emacs, clipboard or cleanup-model calls. They do not alter the voicekey configuration
or running service. Models and public audio live under `/tmp` by default.

The [September 6 report](../docs/asr-benchmark-2026-09-06.md) contains the decision
and limitations. Exact transcripts, timing traces, memory snapshots and model
checksums are in [results/2026-09-06](results/2026-09-06/summary.json).

## Reproduce

Start with the installed voicekey Python environment (sherpa-onnx 1.13.6).
The extra dependencies are isolated so they do not replace service packages:

```sh
bench_dir=/tmp/voicekey-asr-bench
voicekey_python="$HOME/.local/share/voicekey/venv/bin/python"
uv pip install --python "$voicekey_python" --target "$bench_dir/deps" -r benchmarks/requirements.txt
"$voicekey_python" benchmarks/prepare_asr.py --root "$bench_dir"

PYTHONPATH="$bench_dir/deps" "$voicekey_python" benchmarks/share_weights.py \
  --offline "$HOME/.local/share/voicekey/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-non-streaming" \
  --streaming "$bench_dir/sherpa-onnx-nemo-parakeet-unified-en-0.6b-int8-streaming-560ms" \
  --output "$bench_dir/shared"

PYTHONPATH="$bench_dir/deps:benchmarks" "$voicekey_python" -m unittest test_sharing -q
PYTHONPATH="$bench_dir/deps" "$voicekey_python" benchmarks/asr_benchmark.py \
  --variant baseline --output "$bench_dir/baseline.json"
PYTHONPATH="$bench_dir/deps" "$voicekey_python" benchmarks/asr_benchmark.py \
  --variant parakeet-native --output "$bench_dir/parakeet-native.json"
PYTHONPATH="$bench_dir/deps" "$voicekey_python" benchmarks/asr_benchmark.py \
  --variant parakeet-native --smoke --output "$bench_dir/native-smoke.json"
PYTHONPATH="$bench_dir/deps" "$voicekey_python" benchmarks/run_asr_controls.py
"$voicekey_python" benchmarks/collect_asr_results.py
```

Run the inference comparisons serially. `prepare_asr.py` downloads about 850 MB
from the official model release and OpenSLR, validates the published checksums,
then chooses twelve speakers without inspecting any recognizer output. The
reference text is copied verbatim. It requires `ffmpeg` to decode FLAC to WAV.
The existing offline Parakeet and Nemotron model files are also hash-checked
when collecting results.

`--repeat` and `--limit` permit more trials. `--paced` feeds 100 ms frames at
their real audio times. Without it, preview arrival times are simulated from
measured per-call durations; compute time itself is measured directly. The
stock 560 ms Parakeet configuration advances 160 ms per encoder call while
processing 5.6 seconds of preceding context and 400 ms of following context.

## Variants

- `baseline`: production Nemotron previews, production offline Parakeet final
  transcription, using sherpa's native recognizers.
- `parakeet-native`: official buffered streaming Parakeet plus the same
  offline Parakeet final pass; separate native model instances.
- `parakeet-separate`: Python/ONNX Runtime control, separate encoder weights,
  one decoder and one joiner session shared by both modes.
- `parakeet-shared`: the same prototype with exact-byte initializer sharing;
  normal runtime weight packing remains enabled.
- `parakeet-shared-nopack`: shared initializers with weight packing disabled.

The prototype exposes two different encoder graphs through one owner. Large
identical tensors are represented once in a memory-mapped weight bank and the
same `OrtValue` is lent to both sessions. Decoder and joiner graphs are checked
for exact equality before reusing their sessions. Stream state remains separate
from model weights. Small shape constants stay inline for ONNX shape inference.

The native comparison uses sherpa's bundled ONNX Runtime 1.27.1; the experimental
Python runtime uses 1.28.0 and separate feature-extraction bindings. Its three
sharing controls produced identical transcripts to one another. One final
transcript differs from native sherpa in spelling/punctuation, so the prototype
is not established as a drop-in replacement. Do not attribute differences
between those runtime stacks solely to memory sharing.

Memory measurements include warm allocator/workspace retention. PSS is used
for physical-memory comparisons, alongside RSS and swap in the raw files.
Measurements immediately after loading can substantially undercount demand-
paged weights; use the warm measurements after real inference.

## Cleanup context replay

`polish_context_replay.py` is the one script here that exercises cleanup
instead of ASR. It re-sends every persistent-mode cleanup request that carried
context (from the recovery journal) to the configured model on a private port,
and compares the live outcome with what the current code accepts. The replies
are your dictation: they go to `/tmp` (mode 0600), not the repo.
`tests/data/polish_context_replies.json` holds synthetic requests of the same
shapes, with the model's actual replies.

```sh
~/.local/share/voicekey/venv/bin/python benchmarks/polish_context_replay.py
```

## Sources

- [LibriSpeech / OpenSLR 12](https://www.openslr.org/12/), public read English
  speech, CC BY 4.0; dataset by Vassil Panayotov, Guoguo Chen, Daniel Povey and
  Sanjeev Khudanpur. Raw references and clip identifiers are retained for audit.
- [Parakeet Unified](https://huggingface.co/nvidia/parakeet-unified-en-0.6b).
- [Sherpa streaming export](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/scripts/nemo/parakeet-unified-en-0.6b/export_onnx_streaming.py)
  and [streaming implementation](https://github.com/k2-fsa/sherpa-onnx/blob/v1.13.6/sherpa-onnx/csrc/online-recognizer-transducer-nemo-parakeet-unified-impl.h).
- [ONNX Runtime initializer API](https://onnxruntime.ai/docs/api/python/api_summary.html#onnxruntime.SessionOptions.add_initializer)
  and [shared prepacked-weight containers](https://onnxruntime.ai/docs/api/c/struct_ort_api.html).
