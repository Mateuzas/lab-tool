# Spike image: the packages from plan step 1.6 (+ procps for pkill, microsocks for Task 3).
FROM kalilinux/kali-rolling
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openvpn iproute2 iputils-ping curl nmap procps microsocks ca-certificates \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
CMD ["bash"]
