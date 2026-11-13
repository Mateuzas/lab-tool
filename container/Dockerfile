# Lab container: Kali with the VPN client, proxy, capture tools and the lab supervisor.
# Build context is this directory:  docker build -t kali-lab:dev container/
# Code is baked in; only the user's workspace is mounted at /lab at runtime.
FROM kalilinux/kali-rolling

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openvpn iproute2 iputils-ping curl nmap procps microsocks ca-certificates \
      tcpdump tshark iptables \
 && rm -rf /var/lib/apt/lists/*

# Service clients from the agreed MVP tool list (docs/spike-notes.md): the everyday ways a
# beginner talks to a lab machine's services.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-client netcat-openbsd telnet ftp smbclient redis-tools default-mysql-client \
 && rm -rf /var/lib/apt/lists/*

# A lab machine that is switched off sends nothing, so an ssh session to it would freeze. With
# these settings ssh gives up and returns to the prompt after about 15 s of silence.
RUN printf 'Host *\n    ServerAliveInterval 5\n    ServerAliveCountMax 3\n' > /etc/ssh/ssh_config.d/00-lab.conf

COPY --chmod=0755 bin/ /usr/local/bin/
COPY share/ /usr/local/share/

WORKDIR /lab
CMD ["lab-supervisor"]
