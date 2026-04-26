FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# install system dependencies
RUN apt-get update && apt-get install -y \
    python3 python3-pip git curl psmisc \
    && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean

# MAVProxy & Bridge packages
RUN pip3 install mavproxy pymavlink websockets aiohttp future PyYAML --no-cache-dir
RUN npm install -g http-server

# build OpenMCT
RUN git clone https://github.com/nasa/openmct.git /app/openmct-core \
    && cd /app/openmct-core \
    && npm install \
    && npm run build

# Auto-Start
COPY start.sh /start.sh
RUN chmod +x /start.sh

CMD ["/start.sh"]