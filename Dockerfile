ARG DEBIAN_IMAGE=debian:trixie-slim
FROM ${DEBIAN_IMAGE} AS python-build

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential ca-certificates libcups2-dev python3-dev python3-venv \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt ./
RUN python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt
COPY deploy/cups_options_shim.c ./
RUN cc -std=c11 -O2 -Wall -Wextra -Werror -fPIC -shared \
       cups_options_shim.c -o /build/cups_options_shim.so -lcups -ldl

# Fonts Debian does not package (deploy/fonts/sources.txt, checksum-verified)
# plus the household's own fonts from deploy/fonts/private.
FROM ${DEBIAN_IMAGE} AS fonts
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install -y --no-install-recommends cabextract unzip \
    && rm -rf /var/lib/apt/lists/*
COPY deploy/fonts/ /src/
RUN sh /src/install.sh /src /fonts

FROM ${DEBIAN_IMAGE}

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/var/lib/family-print-center \
    DATA_DIR=/var/lib/family-print-center \
    HOST=0.0.0.0 \
    PORT=8080 \
    CUPS_SERVER=127.0.0.1 \
    CUPS_PORT=631 \
    CUPS_USER=familyprint \
    CUPS_PASSWORD_FILE=/run/family-print-center/cups-password \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

# Debian supplies the complete Office conversion and print-filter stack;
# OpenWrt only needs to run the container. The driver packages cover the common
# home brands so the admin page can match most printers without uploads:
# Brother (brlaser), Epson (escpr), HP (hpcups, postscript-hp), Samsung/Xerox
# (splix), host-based lasers (foo2zjs, min12xxw, pxljr), Kodak (c2esp), Fuji
# Xerox, Brother label printers (ptouch), Gutenprint and the OpenPrinting PPDs.
# Add more with: EXTRA_DRIVER_PACKAGES="pkg1 pkg2" sh deploy/build-image.sh
# Fonts: English (Liberation, Carlito/Caladea with Calibri/Cambria widths),
# Japanese (IPA with MS Gothic/Mincho widths, BIZ UD), Traditional and
# Simplified Chinese (Noto CJK, AR PL 楷書/明體, WenQuanYi) and emoji.
ARG EXTRA_DRIVER_PACKAGES=""
RUN printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d \
    && chmod 755 /usr/sbin/policy-rc.d \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       avahi-daemon avahi-utils bash ca-certificates cups cups-client \
       cups-filters cups-ipp-utils dbus fontconfig fonts-liberation fonts-noto-cjk \
       fonts-arphic-ukai fonts-arphic-uming fonts-crosextra-caladea fonts-crosextra-carlito \
       fonts-dejavu-core fonts-ipaexfont-gothic fonts-ipaexfont-mincho fonts-ipafont-gothic \
       fonts-ipafont-mincho fonts-morisawa-bizud-gothic fonts-morisawa-bizud-mincho \
       fonts-noto-color-emoji fonts-open-sans fonts-urw-base35 fonts-wqy-microhei fonts-wqy-zenhei \
       ghostscript libcups2t64 libnss-mdns libreoffice-calc libreoffice-impress \
       libreoffice-writer python3 python3-uno tini util-linux \
       foomatic-db-compressed-ppds openprinting-ppds printer-driver-brlaser \
       printer-driver-c2esp printer-driver-escpr printer-driver-foo2zjs \
       printer-driver-fujixerox printer-driver-gutenprint printer-driver-hpcups \
       printer-driver-min12xxw printer-driver-postscript-hp printer-driver-ptouch \
       printer-driver-pxljr printer-driver-splix \
       ${EXTRA_DRIVER_PACKAGES} \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10000 familyprint \
    && useradd --uid 10000 --gid 10000 --home-dir /var/lib/family-print-center \
       --no-create-home --shell /usr/sbin/nologin familyprint \
    && install -d -o familyprint -g familyprint -m 750 /var/lib/family-print-center \
    && install -d /opt/print-center/defaults /etc/cups/ppd \
    && dbus-uuidgen --ensure=/etc/machine-id

COPY --from=fonts /fonts/ /usr/local/share/fonts/family-print-center/
# Windows/macOS font names in documents map to the installed fonts.
COPY deploy/fonts/60-family-print-center.conf /etc/fonts/conf.d/60-family-print-center.conf
RUN fc-cache -f

COPY --from=python-build /opt/venv /opt/venv
WORKDIR /app
COPY print_center ./print_center
COPY main.py ./main.py
COPY LICENSE NOTICE.md /usr/share/doc/family-print-center/
COPY deploy/cupsd.conf deploy/cups-files.conf /opt/print-center/defaults/
COPY deploy/avahi-daemon.conf /etc/avahi/avahi-daemon.conf
COPY deploy/entrypoint.sh /usr/local/bin/print-center-entrypoint
COPY deploy/healthcheck.py deploy/doctor.py /opt/print-center/
COPY deploy/office_worker.py /app/deploy/office_worker.py
COPY --from=python-build /build/cups_options_shim.so /app/deploy/cups_options_shim.so
RUN chmod 755 /usr/local/bin/print-center-entrypoint \
    && cp /opt/print-center/defaults/cupsd.conf /etc/cups/cupsd.conf \
    && cp /opt/print-center/defaults/cups-files.conf /etc/cups/cups-files.conf \
    && chmod 640 /etc/cups/cupsd.conf /etc/cups/cups-files.conf \
    && chown root:lp /etc/cups/cupsd.conf /etc/cups/cups-files.conf

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "/opt/print-center/healthcheck.py"]
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/print-center-entrypoint"]
CMD ["python", "/app/main.py"]
