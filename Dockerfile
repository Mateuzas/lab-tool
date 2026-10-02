# Spike image: plan step 1.6 packages + procps (pkill), microsocks (Task 3), tcpdump/tshark (Wireshark gate).
FROM kalilinux/kali-rolling
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openvpn iproute2 iputils-ping curl nmap procps microsocks ca-certificates \
      tcpdump tshark iptables \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /lab
CMD ["bash"]
