# Third-party notices

RipeClips is released under the [MIT License](LICENSE). It includes or depends
on third-party software and assets that remain subject to their own licenses.
This file records the components intentionally distributed in this repository
or bundled with a RipeClips build. Dependency lockfiles remain the source of
truth for the complete transitive dependency set.

## Desktop application

- [Tauri](https://tauri.app/) and its official plugins — MIT OR Apache-2.0.
- [Vite](https://vitejs.dev/) — MIT.
- [FFmpeg](https://ffmpeg.org/), used as a locally supplied sidecar — LGPL-2.1+
  or GPL-2.0+, depending on how it is built. RipeClips requires builds with
  `libass` and `libx264`; enabling `libx264` makes the resulting FFmpeg binary
  GPL. Anyone distributing a RipeClips bundle must provide the corresponding
  FFmpeg license notices and comply with the source-offer requirements of that
  build.
- [libass](https://github.com/libass/libass) — ISC.
- [x264](https://www.videolan.org/developers/x264.html) — GPL-2.0-or-later.

The FFmpeg sidecar is generated locally by `app/scripts/setup-ffmpeg.mjs` and
is not committed to this repository. The distributor is responsible for
recording the exact build configuration, source, and notices for that binary.
The `v0.1.0` source release does not distribute an application bundle or
FFmpeg binary.

## Bundled fonts

The font files under `app/public/fonts/` retain their embedded copyright and
license metadata. Sixteen families are distributed under the
[SIL Open Font License 1.1](app/public/fonts/OFL-1.1.txt); Permanent Marker and
Roboto Slab are distributed under the
[Apache License 2.0](app/public/fonts/Apache-2.0.txt).

Reserved Font Names below are transcribed from the bundled files' copyright
records. “None declared” means the bundled font metadata does not declare an
OFL Reserved Font Name; trademark notices embedded in a font still apply.

| Family | Copyright holder / notice | Source | License | Reserved Font Name(s) |
| --- | --- | --- | --- | --- |
| Anton | Copyright 2020 The Anton Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/anton) | OFL-1.1 | None declared |
| Barlow Condensed | Copyright 2017 The Barlow Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/barlowcondensed) | OFL-1.1 | None declared |
| Bree Serif | Copyright © 2011 TypeTogether | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/breeserif) | OFL-1.1 | `Bree`, `Bree Serif` |
| Bungee Tint | Copyright 2023 The Bungee Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/bungeetint) | OFL-1.1 | None declared |
| Chivo Mono | Copyright 2018 The Chivo Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/chivomono) | OFL-1.1 | None declared |
| Encode Sans Condensed | Copyright 2012 The Encode Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/encodesanscondensed) | OFL-1.1 | `Encode Sans` |
| Josefin Sans | Copyright 2010 The Josefin Sans Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/josefinsans) | OFL-1.1 | `Josefin Sans` |
| Lato | Copyright © 2011–2015 tyPoland Łukasz Dziedzic | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/lato) | OFL-1.1 | `Lato` |
| Lexend | Copyright 2019 The Lexend Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/lexend) | OFL-1.1 | None declared |
| Libre Baskerville | Copyright © 2012 Pablo Impallari and Rodrigo Fuenzalida | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/librebaskerville) | OFL-1.1 | `Libre Baskerville` |
| Montserrat | Copyright 2011 The Montserrat Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/montserrat) | OFL-1.1 | None declared |
| Neuton | Copyright 2010 The Neuton Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/neuton) | OFL-1.1 | `Neuton` |
| Patrick Hand | Copyright © 2012 Patrick Wagesreiter | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/patrickhand) | OFL-1.1 | None declared |
| Paytone One | Copyright 2011 The Paytone Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/paytoneone) | OFL-1.1 | None declared |
| Permanent Marker | Copyright © 2010 Font Diner, Inc. | [Google Fonts](https://github.com/google/fonts/tree/main/apache/permanentmarker) | Apache-2.0 | Not applicable |
| Playfair Display | Copyright 2017 The Playfair Display Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/playfairdisplay) | OFL-1.1 | `Playfair Display` |
| Roboto Slab | Copyright 2018 The Roboto Slab Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/apache/robotoslab) | Apache-2.0 | Not applicable |
| Silkscreen | Copyright 2001 The Silkscreen Project Authors | [Google Fonts](https://github.com/google/fonts/tree/main/ofl/silkscreen) | OFL-1.1 | None declared |

**Modification notice:** `Montserrat.ttf` and `RobotoSlab.ttf` were modified by
the RipeClips project into static instances of their upstream variable fonts
for compatibility with libass. Montserrat declares no Reserved Font Name in
the bundled OFL metadata; Roboto Slab is Apache-2.0 licensed and is not subject
to the OFL Reserved Font Name mechanism.

## Transcription service and model ecosystem

- `parakeet/` is derived from
  [groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai](https://github.com/groxaxo/parakeet-tdt-0.6b-v3-fastapi-openai),
  which is MIT licensed, and acknowledges the upstream
  [Shadowfita project](https://github.com/Shadowfita/parakeet-tdt-0.6b-v2-fastapi).
- The transcription service uses [ONNX Runtime](https://onnxruntime.ai/),
  [onnx-asr](https://github.com/istupakov/onnx-asr), and
  [Silero VAD](https://github.com/snakers4/silero-vad), each under its own
  license.
- Parakeet model files are downloaded at runtime and are not included in this
  repository. Their use and redistribution are governed by the model card and
  license supplied by [NVIDIA](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3).

## Python and JavaScript dependencies

The Python services and desktop application use the packages listed in
`highlightr/requirements.txt`, `parakeet/requirements.txt`,
`app/package-lock.json`, and `app/src-tauri/Cargo.lock`. Those packages are
distributed under their respective licenses; see their package metadata and
upstream repositories for complete license texts and notices.
