# Reproducible AppImage build environment.
#
# Ubuntu 22.04 is used deliberately: its glibc (2.35) is old enough for the
# resulting image to run on current Debian/Ubuntu/Fedora releases, while still
# satisfying PyQt6's manylinux_2_34 wheels.
#
#   docker build -f packaging/docker/Dockerfile -t blurb-appimage .
#   docker run --rm -v "$PWD/dist:/out" blurb-appimage

FROM ubuntu:22.04@sha256:076e7bf82319619f28dd0aa9c02ab34b055ad37100395b4f49a2ffba04fb5e11

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        software-properties-common gnupg ca-certificates curl xz-utils \
        file binutils zstd \
        libgl1 libegl1 libopengl0 libdbus-1-3 \
        libfontconfig1 libfreetype6 \
        libxcb1 libxcb-cursor0 libxcb-glx0 libxcb-icccm4 libxcb-image0 \
        libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0 \
        libxcb-xinerama0 libxcb-xkb1 libxcb-util1 libxkbcommon-x11-0 \
    && rm -rf /var/lib/apt/lists/*

# The Ubuntu 22.04 archive only has Python 3.10. Blurb needs 3.12 for the
# current CTranslate2/onnxruntime wheels.
RUN add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv libpython3.12 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src/blurb
COPY . /src/blurb
RUN mkdir -p /out

# The two wrapper scripts write here by default inside the container.
ENV BLURB_OUTPUT_DIR=/out

CMD ["packaging/appimage/build-nvidia.sh"]
