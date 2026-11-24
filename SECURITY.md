# Security

## Know before you run it
`s2kserver` has no authentication or encryption and runs as root on the Raspberry Pi. Run it only on a trusted private network.
Do not expose it to the internet or port-forward it. Use `--host` to bind it to one interface and firewall it to known clients.

## Reporting a problem
Please report vulnerabilities privately through GitHub's
[private vulnerability reporting](https://github.com/neufena/s2k-send/security/advisories/new) rather than a public issue.
This is a hobby project, so there is no response-time guarantee, but reports are taken seriously.
