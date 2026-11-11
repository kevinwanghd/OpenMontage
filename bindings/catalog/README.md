# Provider catalog fragments

Each provider owner writes ONE independent fragment here instead of editing
`bindings/capability_catalog.yaml` (ownership rule: one file, one writer).

`lib/capability_slots.load_catalog()` aggregates `../capability_catalog.yaml`
with every `*.yaml` in this directory, so fragments never conflict.

Fragment shape (validates against `schemas/artifacts/capability_catalog.schema.json`):

```yaml
version: "1.0"
entries:
  - model: dashscope/wan3.0-video
    operation: image_to_video
    native_audio: false
    first_frame_conditioning: true
    output_resolutions: ["480p", "720p", "1080p"]
    source: "Bailian price sheet 2026-11-11 + mock contract"
    verified_at: null
    verification_status: MOCKED
    commercial_rights: "Bailian ToS"
```

Expected fragment: `dashscope.yaml` (WP-F / T10).