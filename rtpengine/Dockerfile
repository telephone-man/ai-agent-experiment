FROM debian:bookworm-slim

# Install required build and runtime dependencies
RUN apt-get update
RUN apt-get install -y --no-install-recommends git dpkg-dev build-essential tcpdump net-tools 
RUN apt-get install -y --no-install-recommends iptables nftables iproute2 debhelper pkg-config
RUN apt-get install -y --no-install-recommends libglib2.0-dev libjson-glib-dev libssl-dev 
RUN apt-get install -y --no-install-recommends libpcre3-dev libxmlrpc-core-c3-dev
RUN apt-get install -y --no-install-recommends libhiredis-dev libcurl4-openssl-dev libevent-dev 
RUN apt-get install -y --no-install-recommends libpcap-dev libsystemd-dev
RUN apt-get install -y --no-install-recommends libavcodec-dev libavfilter-dev lsof libavformat-dev 
RUN apt-get install -y --no-install-recommends libavutil-dev libswresample-dev libopus-dev
RUN apt-get install -y --no-install-recommends libmosquitto-dev libwebsockets-dev libspandsp-dev iputils-ping 
RUN apt-get install -y --no-install-recommends libmariadb-dev-compat libmariadb-dev libiptc-dev gperf 
RUN apt-get install -y --no-install-recommends zlib1g-dev ca-certificates default-libmysqlclient-dev 
RUN apt-get install -y --no-install-recommends dh-sequence-dkms discount libbcg729-dev libbencode-perl 
RUN apt-get install -y --no-install-recommends libcrypt-openssl-rsa-perl dnsutils strace procps 
RUN apt-get install -y --no-install-recommends libcrypt-rijndael-perl libdigest-crc-perl libdigest-hmac-perl
RUN apt-get install -y --no-install-recommends libio-multiplex-perl libio-socket-inet6-perl libjson-perl libmnl-dev libncurses-dev
RUN apt-get install -y --no-install-recommends libnet-interface-perl libnftnl-dev libsocket6-perl libtest2-suite-perl liburing-dev
RUN apt-get install -y --no-install-recommends libxtables-dev pandoc python3-websockets systemd 
RUN apt-get install -y --no-install-recommends libconfig-tiny-perl
RUN apt-get install -y --no-install-recommends libwww-perl
RUN apt-get install -y  netcat-openbsd
RUN rm -rf /var/lib/apt/lists/*

WORKDIR /build
RUN git clone https://github.com/sipwise/rtpengine.git
WORKDIR /build/rtpengine
RUN git checkout tags/mr13.3.1.4 -b build

# Check and build the .deb packages
RUN dpkg-checkbuilddeps || true
RUN dpkg-buildpackage -b -uc -us

WORKDIR /build
RUN if ! dpkg -i ngcp-rtpengine-daemon_*.deb; then \
      apt-get update && apt-get install -f -y; \
    fi
RUN apt-get update && apt-get install -y --no-install-recommends libconfig-tiny-perl
RUN if ! dpkg -i ngcp-rtpengine-utils_*.deb; then \
      apt-get update && apt-get install -f -y; \
    fi && \
    rm -rf /var/lib/apt/lists/*

# check the installed files
RUN dpkg -L ngcp-rtpengine-daemon | grep rtpengine

# Clean apt cache but do not autoremove to avoid removing needed libs
RUN apt-get clean

# Copy configuration and entrypoint
COPY rtpengine.conf /etc/rtpengine.conf.template
COPY rtpengine-entrypoint.sh /usr/local/bin/rtpengine-entrypoint.sh
COPY rtpengine-debug.sh /usr/local/bin/rtpengine-debug.sh
RUN chmod +x /usr/local/bin/rtpengine-debug.sh
RUN ln -s /usr/local/bin/rtpengine-debug.sh /usr/local/bin/rtpengine-debug
RUN chmod +x /usr/local/bin/rtpengine-entrypoint.sh

# rtpengine-ctl returns non-zero when the daemon is not serving CLI requests.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["rtpengine-ctl", "get", "numsessions"]

ENTRYPOINT ["/usr/local/bin/rtpengine-entrypoint.sh"]
