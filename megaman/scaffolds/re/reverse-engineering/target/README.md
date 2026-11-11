Drop the artifact under analysis here (binary, .ipa, .apk, firmware image).

First thing, every time: record its `sha256` in `LOG.md`. Analysis results are
only meaningful against one exact build — captures taken against a different
version of the same app shared *zero* function addresses with the static
analysis in the project this workflow came from.
