# syntax=docker/dockerfile:1
ARG LEGACY_IMAGE=devilbox/php-fpm:5.3-prod@sha256:54b4366ed5e481a6fad90db6629bbc65deff70e7eb86919b1003a5a631bd2368
ARG BUILD_IMAGE=gcc:6.5@sha256:1cd3cd6a2507024ab2a5aa6962098890cb52d529e72a2db2e310270ac9cba004

FROM ${LEGACY_IMAGE} AS legacy
USER root
RUN mkdir -p /usr/local/share/php53-cyber-fix \
    && php -r '$m=get_loaded_extensions();sort($m);echo json_encode($m);' > /usr/local/share/php53-cyber-fix/original-modules.json

FROM ${BUILD_IMAGE} AS builder
COPY --from=legacy /usr/local/ssl/ /usr/local/ssl/
COPY --from=legacy /usr/local/bin/php-config /legacy/php-config
COPY --from=legacy /usr/lib/x86_64-linux-gnu/librecode.so.0* /usr/lib/x86_64-linux-gnu/
COPY build-deps/librecode-dev.deb /tmp/librecode-dev.deb
RUN echo 'b46deaf54cddb4ee6e0b2f5aad2ea12c05cff6b31a3b01a4c193e6656fef3152  /tmp/librecode-dev.deb' | sha256sum -c - \
    && dpkg-deb -x /tmp/librecode-dev.deb /
COPY php-5.3.29/ /build/php/
WORKDIR /build/php
# Preserve the original configure options and extension ABI; do not copy Ubuntu binaries.
RUN set -eu; \
    export CFLAGS='-O2 -g -fcommon'; \
    options=$(/legacy/php-config --configure-options); \
    eval "bash ./configure $options"; \
    make -j2; \
    make install

FROM legacy AS runtime
ARG REVISION=unknown
LABEL org.opencontainers.image.source="https://github.com/luodaoyi/php5.3-cyber-fix" \
      org.opencontainers.image.revision="${REVISION}" \
      org.opencontainers.image.title="PHP 5.3.29 FPM fail-stop hardening" \
      org.opencontainers.image.description="Experimental OOM fail-stop and single-request FPM containment; not a complete PHP 5.3 security fix"
COPY --from=builder /usr/local/bin/php /usr/local/bin/php
COPY --from=builder /usr/local/sbin/php-fpm /usr/local/sbin/php-fpm
COPY UPSTREAM.md /usr/local/share/php53-cyber-fix/UPSTREAM.md
RUN set -eu; \
    php -r '$old=json_decode(file_get_contents("/usr/local/share/php53-cyber-fix/original-modules.json"),true);$now=get_loaded_extensions();$missing=array_diff($old,$now);if(count($missing)){fwrite(STDERR,"Lost modules: ".implode(",",$missing)."\n");exit(1);}'; \
    php -r '$required=array("mysql","mysqli","pdo_mysql","mbstring","curl","json","hash","session","redis","openssl","xml");foreach($required as $e){if(!extension_loaded($e)){fwrite(STDERR,"Missing module: ".$e."\n");exit(1);}}'; \
    php -m > /usr/local/share/php53-cyber-fix/modules.txt; \
    php-fpm -t
