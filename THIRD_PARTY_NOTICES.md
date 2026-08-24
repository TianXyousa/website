# Third-party notices

This project installs or interoperates with third-party software. Those
components are not relicensed by the project's `GPL-3.0-only` notice and
remain governed by their respective licenses.

The principal direct components are listed below. This is a practical notice,
not a replacement for the license files shipped by each dependency or image.

| Component | Role | License |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi) | Web framework | MIT |
| [Uvicorn](https://github.com/Kludex/uvicorn) | ASGI server | BSD-3-Clause |
| [Pydantic](https://github.com/pydantic/pydantic) | Data validation | MIT |
| [python-multipart](https://github.com/Kludex/python-multipart) | Form and upload parsing | Apache-2.0 |
| [biliup](https://github.com/ForgQi/bilibiliupload) | Bilibili uploader CLI | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper) | Speech recognition | MIT |
| [Requests](https://github.com/psf/requests) | HTTP client | Apache-2.0 |
| [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) | FFmpeg discovery/binary package | BSD-2-Clause; the bundled FFmpeg binary retains its applicable FFmpeg license |
| [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder) | Separate recorder container/service | GPL-3.0 |
| [TensorFlow](https://github.com/tensorflow/tensorflow) | Optional GPU inference runtime | Apache-2.0 |
| [inaSpeechSegmenter](https://github.com/lovegaoshi/inaSpeechSegmenter) | Optional GPU segmentation model | MIT |
| [NumPy](https://github.com/numpy/numpy), [pandas](https://github.com/pandas-dev/pandas), [scikit-image](https://github.com/scikit-image/scikit-image) | Optional GPU-model dependencies | BSD-family licenses |
| [pyannote.core](https://github.com/pyannote/pyannote-core) | Optional GPU-model dependency | MIT |
| NVIDIA CUDA base images, CUDA and cuDNN packages | Optional GPU runtime | NVIDIA license terms |

Model weights downloaded at runtime, Bilibili content, ACRCloud results, and
other remote-service data may carry additional terms. Review the applicable
provider terms before redistribution.
