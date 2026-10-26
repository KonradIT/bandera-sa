# Frontera 2.22 model architecture and training evidence

Audited 2026-10-26. The released inference architecture, coefficients, input contract and
app's labeled recording format are recoverable. **The original training-set size and
training recipe were not found in the supplied APK, model metadata or examined public
sources.** The subsequently supplied user manual explicitly describes further training
from users' labeled recordings of missed detections and false alarms (page 12). It does
not identify the corpus used for the exact released model. Model weights alone do not
determine how many examples produced them.

The source is the supplied `Frontera_2.22.apk`, SHA-256
`49163518b5ed665821470a39662f8fd1d93c7dae1572b410a322bc6e1ea1b9ab`.
All five local TFLite files and the golden file match its assets byte for byte. No
models were modified. Counts below describe the exported inference graphs, rather
than an unrecovered training checkpoint.

## Exact model inventory

Every filename has prefix `RY_0_1_6_s_soup8b_`.

| Suffix | File bytes | Unique convolution weights | Biases | Total coefficients | Graph operations |
|---|---:|---:|---:|---:|---:|
| `detect_fp32.tflite` | 1,064,068 | 255,040 | 976 | 256,016 | 88 |
| `rowsums_w8_fp32.tflite` | 56,180 | 10,096 | 120 | 10,216 | 36 |
| `rowsums_w10_fp32.tflite` | 56,988 | 10,096 | 120 | 10,216 | 39 |
| `rowsums_w12_fp32.tflite` | 56,988 | 10,096 | 120 | 10,216 | 39 |
| `rowsums_w16_fp32.tflite` | 56,988 | 10,096 | 120 | 10,216 | 39 |
| `golden.bin` | 88,932 | — | — | — | — |

The encoder's learned coefficients are identical across all four window sizes.
One encoder plus detector has **266,232 exported convolution coefficients**, occupying
**1,064,928 bytes** in FP32. Do not count the four encoder exports as four separately
learned networks. Shared detector tensors are counted once even when invoked at several
frequency scales.

These are the constant weights and biases used by ordinary convolution operations.
The original trainable parameter count may differ because export can fold normalization
parameters into convolutions or remove training-only operations. The inference files do
not retain an optimizer state or a count of original training variables.

Excluded from those totals are 170 fixed scalar elements in each encoder, including
binary selection kernels and zero biases used for temporal reductions, and 17 fixed
elements in the detector for padding, resizing and distance arithmetic. All neural
weights/activations are FP32; integer tensors configure shape/padding. There are no
quantization scales or zero points.

## Input sizes and the different meanings of sample count

| Quantity | Verified meaning |
|---|---|
| Training examples / recordings | Unknown |
| Bins per sweep | 600 power levels, spaced 3 MHz apart |
| Frequency span | Nominal 1800 MHz window |
| Sweeps per inference | W = 8, 10, 12 or 16 |
| Raw values per inference | 4,800 / 6,000 / 7,200 / 9,600 power values |
| Encoder input | FP32 `[1, W, 600, 3]`; 57,600–115,200 bytes |
| Encoder output | FP32 `[1, 1, 300, 128]`; 153,600 bytes |
| Detector input | FP32 `[1, 1, 300, 129]`; 154,800 bytes |
| Detector output | FP32 `[1, 1, 263, 12]`; 12,624 bytes |
| App recording default | 600 sweeps per recording; configurable from 1 to 4000 |
| Instrument `samples=20` | Acquisition parameter per frequency point, not training examples |
| Golden fixture | One 16-sweep input with four reference outputs |

The leading input dimension of 1 is the exported **inference batch size**. It does not
reveal the training batch size. W is the temporal window size, not dataset size or number
of training epochs. The two occurrences of 600 in the recording default refer to different
axes: 600 consecutive sweeps, each containing 600 frequency bins.

The runtime constructs three channels: level above an estimated floor, deviation from the
per-frequency temporal median, and a smoothed spectral plateau above that floor. No I/Q,
image/video pixels, timestamp or absolute frequency coordinate enters the neural graph.
See [`encode()`](../sdr/bandera/model.py) for the recovered normalization. These are facts
about the deployed input contract, not proof of the complete training preprocessing or
augmentation pipeline.

## Recovered architecture

The encoder is a small convolutional network across frequency. All learned kernels have
temporal height 1, so they process rows independently before computing temporal statistics.
Every learned encoder convolution is followed by SiLU, implemented as `x * sigmoid(x)`.

| Encoder layer | Channels | Frequency kernel | Stride | Dilation | Output frequency positions |
|---|---|---:|---:|---:|---:|
| Conv0 | 3 → 16 | 5 | 1 | 1 | 600 |
| Conv1 | 16 → 16 | 5 | 1 | 1 | 600 |
| Conv2 | 16 → 24 | 3 | 1 | 2 | 600 |
| Conv3 | 24 → 32 | 3 | 2 | 1 | 300 |
| Conv4 | 32 → 32 | 5 | 1 | 1 | 300 |

It concatenates sum, sum of squares, adjacent-row product sum and maximum across the W rows.
The app converts these to mean, variance, maximum and lag covariance, then adds
`1/sqrt(W)`, yielding 129 channels.

The detector is a **1-D feature-pyramid network with top-down and bottom-up fusion**:

- A kernel-1 projection maps 129 channels to 64.
- A backbone produces 150, 75 and 38 frequency positions using stride-2 convolutions.
  Kernel-5 processing uses dilation 1 at the first scale and 2 at the latter scales.
- Top-down bilinear resizing and concatenation fuse 38 → 75 → 150 positions.
  A bottom-up path then fuses 150 → 75 → 38, with 64-channel outputs.
- Each scale uses two shared kernel-3 stem convolutions and a shared kernel-1 head for
  objectness plus nine classes. Separate kernel-1 heads predict two distances per scale.
- There are 25 detector convolution invocations but 19 distinct weight tensors. The
  150 + 75 + 38 positions give 263 predictions.
- Each prediction contains an objectness logit, nine class logits and left/right
  distances. The distance transform is `exp(min(raw, 6)) * stride - stride/2`, using
  strides 4, 8 and 16. The app applies score thresholds and nonmaximum suppression.

Classes, in output order:
`analog, dji, autel, siyi, wifi, lte, other_signals, gnss, wfb_ng`.
This structure establishes frequency-region detection. It does not establish a specific
YOLO release, pretrained backbone, training loss or target-assignment algorithm.

The exported convolution operations require approximately 58.64 million MACs at W8 and
89.89 million at W16, including the detector. This excludes preprocessing, nonlinearities,
non-convolution reductions and decoding; it is not a hardware latency measurement. The largest individual
encoder FP32 intermediate is 462,336 bytes at W8 and 924,672 at W16. Model file size is
therefore not its total working-memory requirement.

## What the APK reveals about data collection

Frontera 2.22 includes a recording and labeling workflow that can produce data appropriate
for this type of detector:

1. Choose a center frequency and a recording length, defaulting to 600 sweeps. The span is
   center ±900 MHz. The saved grid has 600 bins at 3 MHz spacing.
2. Record power sweeps with parameters including `samples=20`, `timeoutMs=400` and
   attenuation 0 dB in this recording path.
3. Attach class labels to frequency intervals. Each box has `lo_mhz`, `hi_mhz` and `label`;
   these boxes have no temporal coordinates.
4. Save paired `.npy` and `.json` files under `Downloads/dataset_regions/regions/data`.
   The share action packages the pair as a ZIP.

The NPY format is version 1.0, little-endian float32, C order, shape `[n_sweeps, 600]`.
A default recording holds **360,000 power values / 1,440,000 payload bytes**, before the
NPY header and JSON. The sidecar includes filename, label, center/start/stop/step frequency,
attenuation, timestamp, sweep/bin counts, region boxes, instrument samples/timeout,
device name and a region flag.

The annotation UI offers the nine model classes **plus `dvbt`**. That extra option is not
a tenth class in the released detector. Device metadata distinguishes `frontera` and
`horizont-12`. The UI also warns about floor changes at the 6100 MHz mixer boundary and
about not labeling mirror spurs above 6000 MHz as signals.

The APK establishes the labeled recording capability. The user manual additionally
documents its purpose as a source for further training (see below). Neither source
establishes which recordings trained this exact model, how many were collected, whether
additional collection tools existed, or whether synthetic data/augmentation were used.
It is possible to reproduce the collection format without knowing the original dataset.

Decompiler evidence, relative to the decoded source root:

| Location | Evidence |
|---|---|
| `v8/e.java:174` | Default 600-sweep input |
| `u8/g.java:238` | Recording parameters and accepted 1–4000 sweep count |
| `q8/f.java:17` | Center ±900 MHz recording span |
| `u8/j.java:86` | Requires 600 bins; saves paired NPY/JSON files |
| `b8/a.java:41` | NPY dtype, shape, order and version |
| `com/example/frontera_region_recording/domain/model/RegionSidecar.java:246` | Sidecar field names |
| `q8/c.java:22` | Recording label vocabulary |
| `o8/b.java:168` | Export directory |
| `z6/e.java:33` | Instrument identities |
| `u8/g.java:287` | Mixer-boundary and spur annotation warnings |

These names are specific to the supplied 2.22 APK. The local decompiler output is under
`%TEMP%/bandera-frontera-jadx/sources`; it is not a source dependency of this repository.

## Export provenance and naming limits

All five models have description `MLIR Converted.` and only two metadata entries:

- `min_runtime_version`: detector `2.2.0`, encoders `1.14.0`. These are runtime compatibility
  minima, not the training framework/exporter versions or dates.
- `keep_stablehlo_constant`: `true`.

Tensor names contain `litert_torch._convert.to_channel_last_io.ChannelLastIOWrapper`,
`torch.nn.modules.conv.Conv2d`, `FusedEncoder`, `FusedDetector`, `RowSums` and `SweepStats`.
This strongly identifies a **PyTorch / LiteRT Torch export path**. It does not prove the
original training framework, exact software version or whether weights were imported
from another framework before export. Earlier repository wording, "trained in PyTorch,"
was stronger than this evidence supports.

The tag `RY_0_1_6_s_soup8b` is not a training manifest. In particular, `8b` does not mean
eight billion parameters. "Soup" could allude to weight averaging, but neither eight
checkpoints nor a particular averaging recipe is established. The
[model-soups paper](https://proceedings.mlr.press/v162/wortsman22a.html) explains that
general technique; it is not evidence that Frontera used it.

## What the golden file contains

The 88,932-byte file has an `RBG1` header, four dimensions, 16 × 600 FP32 sweep levels,
and four reference output arrays for W8/W10/W12/W16. Each output is 263 × 12 FP32 values
preceded by its W. There is no trailing payload.

The app checks centered crops of the same 16-sweep input. This is one regression fixture,
not evidence of 16 training examples, four training sets, class coverage or test accuracy.
Its originating capture and relationship to any training split are undisclosed.

## Public-source findings and unresolved questions

The [23 October 2026 developer announcement](https://t.me/kbrtb_uav/126) says the team
developed its own ML model for video-signal classification, names supported SA6/Arinst R2/
Frontera analyzers and lists detected signal families. It supplies no dataset size,
training procedure or evaluation metrics. Supported instruments do not prove which
instruments collected the training corpus. The original APK points to
[NPO APOGEI's Telegram account](https://t.me/NPO_APOGEI); its
[22 October announcement](https://t.me/NPO_Apogei/11) makes the same own-model claim.
The channel also publishes the [2.22 APK](https://t.me/NPO_Apogei/13) and
[Frontera-6 hardware specifications](https://t.me/NPO_Apogei/9), including a fixed
200 kHz IF bandwidth. These specifications do not identify the training acquisition setup.

Exact model-tag/package searches and English/Russian searches for training, samples and
datasets found no independent model card, training repository or dataset tied to these
assets. Public КБ РТБ history through post 129 and the original NPO APOGEI channel's
visible posts 1–14 were checked; search results reproducing this repository were excluded
as circular evidence. This is a bounded search result, not proof that no other disclosure exists.

### User manual: confirmed further-training workflow

The user supplied the PDF attached to the developers'
[26 October manual post](https://t.me/NPO_Apogei/14), allowing all **13 pages** to be read.
Its filename is `Frontera_ML инструкция пользователя v2.pdf`, length **1,250,080 bytes**,
SHA-256 `6ec396cda59b17b9a5e5a01f12bfad03ef7ec9814babad24cd9c1d9d0916a185`.
The cover describes Frontera 2.0, revision September 2026; the PDF creation metadata is
26 October 2026. Neither is a model training date. The document does not identify the
`RY_0_1_6_s_soup8b` tag.

**Page 12, section 9, explicitly says:** "По таким записям дообучается распознавание."
In context: recognition is further trained using recordings submitted for missed detections
and false alarms. This is a developer statement of an iterative data-collection and
further-training process, stronger evidence than the APK's collection capability alone.

The prescribed process on pages 12–13 is:

1. Record an example that should be detected but is missed, or that incorrectly triggers
   an alert.
2. Center the recording on the signal, keep the count at **600 sweeps**, check the preview,
   and keep the RF conditions unchanged until the capture finishes.
3. Manually draw frequency boxes and choose the correct signal classes.
4. For false alarms with no drone present, label the actual observed signal, such as Wi-Fi
   or LTE, rather than accepting the erroneous detection label.
5. Share the recording ZIP with the developers, accompanied by a description of the signal,
   whether a drone was present and where the recording was made.

Page 13 confirms storage under `Download/dataset_regions/regions/data`. Together with the
APK's NPY/JSON implementation, this documents a human-labeled spectrum collection workflow
that includes difficult negatives. Interpreting it as error-driven supervised data
collection is reasonable; a specific active-learning, sampling or optimization algorithm
is not disclosed. The manual does not say that training runs on the phone.

The 600-sweep recommendation is **per submission**, not a count of the training corpus.
Combining it with the recovered 600-bin FP32 recording format gives 360,000 power values
and 1,440,000 numeric payload bytes per recommended capture. The number of collected or
accepted captures, their conversion into training windows, and their inclusion in the
exact released checkpoint remain unknown.

Other relevant scope distinctions:

- Page 4 describes 1800 MHz analysis windows and a four-position speed/accuracy slider.
  Its approximate 14-second full-range scan claim is operating behavior, not a training
  duration or a measured accuracy benchmark.
- Pages 7–8 describe app-level confirmation: confident signals can alert immediately;
  uncertain signals need detection in two of three overview cycles. Displayed alerts
  therefore include temporal decision rules beyond a single neural-network prediction.
- The manual supplies no total training sample count, dataset volume, class balance,
  optimizer, loss, epochs, batch size, split or held-out evaluation metrics.

No recordings were sent to the developers during this audit.

Still unknown:

- Number of recordings, sweeps, independent windows and annotated regions used in training.
- Corpus bytes/duration, class balance, SNR distribution, devices, sites and capture dates.
- Training/validation/test splits and whether overlapping windows crossed split boundaries.
- Original training batch size, epochs/steps, optimizer, learning-rate schedule and loss.
- Augmentation, synthetic examples, initialization/pretraining and checkpoint selection.
- Whether weight averaging was used and, if so, which checkpoints and recipe.
- Held-out precision/recall, false-positive rate, per-class metrics and deployment calibration.

Resolving these requires original training artifacts (data manifests, scripts, logs or
checkpoints containing provenance) or a technical disclosure from the developers. Parameter
count, recording defaults, runtime thresholds and the golden test cannot substitute for them.

## Local audit artifacts

Read-only extraction results are in ignored `build/model-audit/`:
`summary.json`, `audit.json`, `architecture.txt`, `apk-assets.json` and `apk-hashes.json`.
The corresponding `audit.py`, `apk_asset_audit.py` and `apk_hashes.py` reproduce the local
inspection using the existing `sdr/.venv` environment. The full graph includes every
operator, tensor shape, constant hash and model metadata value.
`read_manual.py` extracts the supplied PDF into `manual.txt` and `manual.json`, preserving
page references and PDF metadata; pages 12–13 were also rendered for visual inspection.

For example, from Git Bash at the repository root:

```bash
sdr/.venv/Scripts/python build/model-audit/audit.py
```

No model execution, device access, firmware flash or training was needed for this audit.
