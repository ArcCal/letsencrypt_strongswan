ARG TEST_IMAGE=ikev2-vpn-test:local
FROM ${TEST_IMAGE}
RUN IKEV2_AUDIT_FIXTURES=1 python3 /repo/tests/runtime_setup.py && \
    systemctl enable strongswan.service ikev2-vpn-firewall.service ikev2-vpn-renew.timer && \
    systemctl disable certbot.timer
ENV container=docker
CMD ["/lib/systemd/systemd"]
