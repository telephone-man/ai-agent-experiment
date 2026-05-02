# syntax=docker/dockerfile:1.7
FROM debian:bookworm

# Override this build arg locally to test another upstream ref.
ARG MOD_AUDIO_STREAM_REF=v1.0.3
ARG MOD_PIPER_TTS_VERSION=a338f070e030153305f1959ab500705b4128dd67

ENV PATH="/usr/lib/ccache:${PATH}" \
    CCACHE_DIR=/root/.cache/ccache

COPY ./conf/modules.conf /modules.conf

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get -y install \
      autoconf \
      automake \
      build-essential \
      ca-certificates \
      ccache \
      cmake \
      curl \
      espeak-ng \
      espeak-ng-data \
      flite1-dev \
      git \
      iproute2 \
      libavformat-dev \
      libcurl4-openssl-dev \
      libedit-dev \
      libespeak-ng1 \
      libevent-dev \
      libjpeg-dev \
      libldns-dev \
      liblua5.2-0 \
      liblua5.2-dev \
      libopus-dev \
      libpcre3-dev \
      libsndfile1-dev \
      libspeex-dev \
      libspeexdsp-dev \
      libsqlite3-dev \
      libssl-dev \
      libswresample-dev \
      libswscale-dev \
      libtiff5-dev \
      libxml2-dev \
      libxml2-utils \
      nano \
      net-tools \
      ntpdate \
      pkg-config \
      sngrep \
      tcpdump \
      unixodbc-dev \
      unzip \
      uuid-dev \
      yasm \
      zlib1g-dev

RUN --mount=type=cache,target=/root/.cache/ccache,sharing=locked \
    cd /usr/src/ && \
    git clone --depth 100 --branch v2.0.7 https://github.com/signalwire/libks.git && \
    cd libks && \
    cmake . && cmake --build . --parallel "$(nproc)" && cmake --install . && \
    cd /usr/src/ && \
    git clone --depth 100 --branch v2.0.2 https://github.com/signalwire/signalwire-c.git && \
    cd signalwire-c && \
    cmake . && cmake --build . --parallel "$(nproc)" && cmake --install . && \
    cd /usr/src/ && \
    git clone --depth 1 --branch v1.13.17 https://github.com/freeswitch/sofia-sip.git && \
    cd sofia-sip && \
    ./bootstrap.sh && ./configure && make -j"$(nproc)" && make install && \
    cd /usr/src/ && \
    git clone https://github.com/freeswitch/spandsp.git && \
    cd spandsp && git checkout 0d2e6ac65e && \
    ./bootstrap.sh && ./configure && make -j"$(nproc)" && make install && \
    ldconfig

RUN --mount=type=cache,target=/root/.cache/ccache,sharing=locked \
    cd /usr/src/ && \
    git clone --depth 1 --branch v1.10.12 https://github.com/signalwire/freeswitch.git freeswitch && \
    cd freeswitch && \
    git config pull.rebase true && \
    cp /modules.conf /usr/src/freeswitch/modules.conf && \
    cd /usr/src/ && \
    git init mod_piper_tts && \
    cd mod_piper_tts && \
    git remote add origin https://github.com/akscf/mod_piper_tts.git && \
    git fetch --depth 1 origin "${MOD_PIPER_TTS_VERSION}" && \
    git checkout --detach FETCH_HEAD && \
    mkdir -p /usr/src/freeswitch/src/mod/asr_tts/mod_piper_tts && \
    cp -R sources/. /usr/src/freeswitch/src/mod/asr_tts/mod_piper_tts/ && \
    cd /usr/src/ && rm -rf mod_piper_tts && \
    cd /usr/src/freeswitch && \
    ./bootstrap.sh -j && ./configure && make -j"$(nproc)" && make install && \
    ldconfig

RUN --mount=type=cache,target=/root/.cache/ccache,sharing=locked \
    cd /usr/src/ && \
    git init mod_audio_stream && \
    cd mod_audio_stream && \
    git remote add origin https://github.com/amigniter/mod_audio_stream.git && \
    git fetch --depth 1 origin "${MOD_AUDIO_STREAM_REF}" && \
    git checkout --detach FETCH_HEAD && \
    git submodule update --init --recursive --depth 1 && \
    mkdir build && cd build && \
    PKG_CONFIG_PATH=/usr/local/freeswitch/lib/pkgconfig cmake -DCMAKE_BUILD_TYPE=Release .. && \
    cmake --build . --parallel "$(nproc)" && cmake --install . && \
    ldconfig

ARG PIPER_VERSION=2023.11.14-2
ARG PIPER_VOICES_REF=v1.0.0
ARG PIPER_VOICE_EN=en_GB-northern_english_male-medium
ARG PIPER_VOICE_FR=fr_FR-siwis-medium

RUN --mount=type=cache,target=/root/.cache/piper-downloads,sharing=locked \
    ARCH="$(dpkg --print-architecture)" && \
    case "$ARCH" in \
      amd64) PIPER_ARCH=x86_64 ;; \
      arm64) PIPER_ARCH=aarch64 ;; \
      *) echo "Unsupported Piper architecture: $ARCH" >&2; exit 1 ;; \
    esac && \
    mkdir -p "/opt/piper/voices/${PIPER_VOICE_EN}" "/opt/piper/voices/${PIPER_VOICE_FR}" && \
    curl -fsSL "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_${PIPER_ARCH}.tar.gz" -o /root/.cache/piper-downloads/piper.tar.gz && \
    tar -xzf /root/.cache/piper-downloads/piper.tar.gz -C /opt && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/${PIPER_VOICES_REF}/en/en_GB/northern_english_male/medium/${PIPER_VOICE_EN}.onnx" -o "/opt/piper/voices/${PIPER_VOICE_EN}/${PIPER_VOICE_EN}.onnx" && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/${PIPER_VOICES_REF}/en/en_GB/northern_english_male/medium/${PIPER_VOICE_EN}.onnx.json" -o "/opt/piper/voices/${PIPER_VOICE_EN}/${PIPER_VOICE_EN}.onnx.json" && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/${PIPER_VOICES_REF}/fr/fr_FR/siwis/medium/${PIPER_VOICE_FR}.onnx" -o "/opt/piper/voices/${PIPER_VOICE_FR}/${PIPER_VOICE_FR}.onnx" && \
    curl -fsSL "https://huggingface.co/rhasspy/piper-voices/resolve/${PIPER_VOICES_REF}/fr/fr_FR/siwis/medium/${PIPER_VOICE_FR}.onnx.json" -o "/opt/piper/voices/${PIPER_VOICE_FR}/${PIPER_VOICE_FR}.onnx.json"

RUN rm -rf /usr/local/freeswitch/conf/*
COPY conf /usr/local/freeswitch/conf.template

COPY freeswitch-entrypoint.sh /usr/local/bin/freeswitch-entrypoint.sh
RUN chmod +x /usr/local/bin/freeswitch-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/freeswitch-entrypoint.sh"]
