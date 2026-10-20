# CUTROOM as a single Linux file

`CutRoom-<version>-x86_64.AppImage` is one executable for 64-bit Linux. It carries its own Python 3.12, the Python dependencies from `requirements.txt` and an FFmpeg 7.1 build with libx264, libass and libvidstab, so nothing else needs installing. Speech and AI models are not included; they download on first use, as in a source checkout.

This is a **development build**: unsigned, and tested by an automated synthetic editing session rather than on physical desktops. The source-checkout path in [platform setup](../../docs/PLATFORMS.md) is unchanged.

## Get and run it

1. Open the repository's **Releases** page and download **CutRoom-1.2-beta-x86_64.AppImage** from the `linux-appimage-dev` pre-release. `SHA256SUMS` sits beside it.
2. In a terminal:

   ```sh
   chmod +x CutRoom-1.2-beta-x86_64.AppImage
   ./CutRoom-1.2-beta-x86_64.AppImage
   ```

3. The editor opens at **http://127.0.0.1:8765**. Keep the terminal open while editing and press **Control-C** to stop it. Starting the file again while it runs only reopens the browser.

AppImages need FUSE 2. If the file reports a missing FUSE library, install it (`sudo apt install libfuse2` on Ubuntu 22.04, `sudo apt install libfuse2t64` on Ubuntu 24.04), or run it without FUSE:

```sh
./CutRoom-1.2-beta-x86_64.AppImage --appimage-extract-and-run
```

## Where things go

The file itself is read-only. Projects, exports and caches are stored in `~/.local/share/cutroom`. Set `CUTROOM_DATA_DIR` to use another folder and `CUTROOM_PORT` to use another port. Set `CUTROOM_FFMPEG` and `CUTROOM_FFPROBE` to use your own FFmpeg instead of the bundled one.

## How it is built

[`.github/workflows/linux-appimage.yml`](../../.github/workflows/linux-appimage.yml) builds it on every push that changes this folder, and can be started by hand. It:

1. Downloads a relocatable Python 3.12 and a static FFmpeg 7.1 build, verifying the FFmpeg archive against its published checksum.
2. Runs the full regression suite against that FFmpeg.
3. Runs [`build_appdir.sh`](build_appdir.sh) to assemble the folder that becomes the image.
4. Smoke-tests that folder with its files read-only, then builds the AppImage and smoke-tests the file. [`scripts/smoke_linux_appimage.py`](../../scripts/smoke_linux_appimage.py) imports a generated clip, cuts it, adds captions, exports, checks the caption is drawn on the picture, runs a stabilization job, restarts at once on the same port and reopens the project.
5. Publishes the file as the rolling `linux-appimage-dev` pre-release.

To build one yourself, give `build_appdir.sh` a Python 3.12 install in `PYTHON_HOME` and a folder holding `ffmpeg` and `ffprobe` in `FFMPEG_DIR`, then run `appimagetool` on the resulting `build/linux/CutRoom.AppDir`.

## Licenses

The bundled FFmpeg is a GPL build; its license text is inside the image at `usr/share/doc/cutroom/FFMPEG_LICENSE.txt`, and its source is published by the builder named in the workflow. CUTROOM itself is MIT licensed; see [third-party notices](../../docs/THIRD_PARTY_NOTICES.md).
