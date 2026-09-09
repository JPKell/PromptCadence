# LAN access — the four applications from other machines on your network

**Audience:** an operator who wants FreeWeight, LoadCoach, IdeaPress and PromptCadence reachable
from a laptop, phone or second workstation on the same LAN. **Reference machine:** `Jordan-main`,
`10.77.10.84`, Ubuntu with systemd and Avahi (`jordan-main.local` resolves on the LAN).

**Read first:** [ADR-0026](adr/0026-local-http-hardening.md) (the Host allowlist, CSRF, why TLS),
each app's `docs/security.md` (its own exposure rules), `docs/scripts/expose_on_lan.sh` (what this
document describes, as a script).

---

## 1. The shape, and why

Every app is **local-first**: bound to `127.0.0.1`, no credentials, safe only because nothing off
the machine can reach it. Each will refuse to start on a non-loopback bind without a Host
allowlist and a credential (`INSECURE_BINDING`). Two facts decide the shape:

1. **The web UIs need TLS from any hostname but `localhost`.** The CSRF cookie every form uses is
   `__Host-`-prefixed and `Secure`; browsers set it over `http://localhost` and over `https://`,
   and over nothing else. Plain `http://10.77.10.84:8765` renders the page and then refuses every
   form post with `403 CSRF_FAILED`. This is not configurable, on purpose.
2. **IdeaPress has no authentication at all** (single-user by design, security.md "What IdeaPress
   does not do"). Something in front of it has to ask who you are.

So: **the apps stay on loopback, and one reverse proxy does both jobs.** Caddy terminates TLS
with its own local certificate authority, asks for a username and password, and forwards to
`127.0.0.1:<app port>`. Nothing about the apps' security posture changes — they still bind
loopback, still require no app-level token — except that each app's `allowed_hosts` names this
machine so the proxied `Host` header passes the rebinding check.

| App | Loopback (unchanged) | On the LAN, through Caddy |
|---|---|---|
| FreeWeight | `127.0.0.1:8765` | `https://jordan-main.local:9765` |
| LoadCoach | `127.0.0.1:8766` | `https://jordan-main.local:9766` |
| IdeaPress | `127.0.0.1:8767` | `https://jordan-main.local:9767` |
| PromptCadence | `127.0.0.1:8768` | `https://jordan-main.local:9768` |

Each is also reachable as `https://10.77.10.84:<port>` for a client without mDNS. The
composition between apps (IdeaPress → LoadCoach, PromptCadence → LoadCoach) keeps using loopback
and is untouched.

**Not chosen:** binding each app to `10.77.10.84` with `auth.tokens` / `token create`. It works
for API callers with a bearer token, still needs TLS for every UI, exposes four HTTP ports instead
of one proxy, and does nothing for IdeaPress. Keep it for a headless API-only host.

---

## 2. Turning it on

```bash
~/ai/suite/docs/scripts/expose_on_lan.sh          # asks for a username and password
~/ai/suite/docs/scripts/expose_on_lan.sh --off    # stop everything; leaves configuration in place
```

What it does, in order — each step is what you would do by hand:

1. `apt-get install caddy` if absent; `loginctl enable-linger` so user services outlive logins.
2. Hashes the password with `caddy hash-password`. The plaintext is never written anywhere.
3. Appends a `[server]` block to each app's `~/.config/<app>/config.toml`:
   `allowed_hosts = ["localhost", "127.0.0.1", "jordan-main.local", "10.77.10.84"]`, and for
   LoadCoach and PromptCadence `trusted_proxies = ["127.0.0.0/8"]` so their rate-limit and
   failed-auth brakes key on the real client address from `X-Forwarded-For`, not on Caddy's.
   An existing `[server]` block is left alone and named.
4. Writes `/etc/caddy/Caddyfile`: one `https://` site per app with `tls internal`, `basicauth`,
   `reverse_proxy 127.0.0.1:<port>`; plus a plain-HTTP site on `:9780` serving **only** the CA's
   public `root.crt` from `/etc/caddy/public` (never the CA directory, which holds private keys).
   Validates, enables and reloads Caddy; waits for the local CA to exist.
5. Writes four `systemd --user` units (`ExecStart=<venv>/bin/<app> serve`, `Restart=on-failure`),
   enables and starts them, waits for each `/api/v1/health`.
6. Checks every app through Caddy with the credentials (expects `200`) and without (expects `401`).

Re-running is safe. The Caddyfile is backed up once per day before it is rewritten.

---

## 3. Trusting the certificate on each client

Caddy's `tls internal` signs with a CA that only this machine knows. Every client device must
trust that CA's root certificate **once**; until it does, the browser shows a certificate warning,
and — worse than the warning — treats the origin as insecure, so the `__Host-` cookie is never set
and every form fails even after you click through. Trust the root; do not click through.

**Get the certificate:** `http://jordan-main.local:9780/root.crt` (or `http://10.77.10.84:9780/root.crt`)
from the client, or copy `~/jordan-main.local-root.crt` from the server. It is the CA's public
certificate; it contains no secret. Its lifetime is 10 years; the per-site leaf certificates Caddy
issues under it renew themselves.

**Verify before trusting** — on the server, print the fingerprint and compare it on the client:

```bash
openssl x509 -in /etc/caddy/public/root.crt -noout -fingerprint -sha256 -subject
```

### Linux (Debian/Ubuntu — system store, curl, Chrome, Chromium)

```bash
sudo cp root.crt /usr/local/share/ca-certificates/jordan-main-caddy.crt   # must end in .crt
sudo update-ca-certificates
```

Firefox keeps its own store: `Settings → Privacy & Security → Certificates → View Certificates
→ Authorities → Import…`, pick `root.crt`, tick *Trust this CA to identify websites*. (Or run
`certutil -d sql:$HOME/.mozilla/firefox/<profile> -A -t "C,," -n jordan-main-caddy -i root.crt`
from `libnss3-tools`.) Snap-packaged Firefox and Chromium may ignore the system store; use the
in-browser import for those.

Fedora/RHEL: `sudo cp root.crt /etc/pki/ca-trust/source/anchors/ && sudo update-ca-trust`.

### macOS (Safari, Chrome; Firefox needs its own import as above)

```bash
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain root.crt
```

Or double-click `root.crt` → Keychain Access → *System* keychain → double-click the certificate
→ *Trust* → *When using this certificate: Always Trust*. Restart the browser.

### Windows (Edge, Chrome; Firefox needs its own import)

Administrator PowerShell or Command Prompt:

```powershell
certutil -addstore -f Root root.crt
```

Or double-click `root.crt` → *Install Certificate…* → *Local Machine* → *Place all certificates in
the following store* → **Trusted Root Certification Authorities**.

### iOS / iPadOS

1. Open `http://jordan-main.local:9780/root.crt` in Safari (not Chrome) → *Allow* the profile
   download.
2. *Settings → General → VPN & Device Management → Downloaded Profile → Install* (device passcode).
3. **Then, separately:** *Settings → General → About → Certificate Trust Settings* → turn on
   *Enable Full Trust for Root Certificates* for the Caddy root. Without step 3 the profile is
   installed but not trusted for websites.

### Android

*Settings → Security & privacy → More security & privacy → Encryption & credentials → Install a
certificate → CA certificate* → *Install anyway* → pick the downloaded `root.crt`. (Menu names
vary by vendor and version; the store is always "CA certificate", not "VPN & app user
certificate".) Chrome trusts user CAs; some apps built with a strict network-security config do
not — the four web UIs are fine in Chrome and Firefox.

### The server itself

```bash
sudo cp ~/jordan-main.local-root.crt /usr/local/share/ca-certificates/jordan-main-caddy.crt
sudo update-ca-certificates
```

Only needed for browsing the `https://jordan-main.local:97xx` URLs *from* the server; `localhost`
keeps working over plain HTTP.

### Check it worked

```bash
curl -u <user> https://jordan-main.local:9766/api/v1/health     # no -k, no warning: trusted
```

A browser at `https://jordan-main.local:9767` shows a padlock, asks for the password once, and
IdeaPress's forms submit.

---

## 4. Operating it

* **Change the password:** re-run the script with `LAN_USER=… LAN_PASSWORD=…`; it rewrites the
  Caddyfile and reloads Caddy. Add more users by adding lines under `basicauth` in
  `/etc/caddy/Caddyfile` (`caddy hash-password` for each) and `sudo systemctl reload caddy`.
* **Logs:** `journalctl --user -u freeweight -f` (and the other three); `sudo journalctl -u caddy -f`.
* **Rotate the CA** (a device you no longer control has the root): `sudo systemctl stop caddy`,
  delete `/var/lib/caddy/.local/share/caddy/pki/authorities/local/`, start Caddy, re-run the
  script, re-trust on every client.
* **Turn it off:** `expose_on_lan.sh --off`. The apps' `[server]` blocks and the Caddyfile stay;
  the apps are simply not running and Caddy is stopped. Delete the `[server]` block to return an
  app to its pre-LAN configuration exactly.
* **API clients on the LAN** (a script on the laptop calling LoadCoach): send HTTP basic auth to
  Caddy — `curl -u user:pass https://jordan-main.local:9766/api/v1/…`. LoadCoach's and
  PromptCadence's own bearer tokens are not required while they bind loopback; add them
  (`loadcoach token create …`) only if you later move an app off loopback.

## 5. What this does not do

* No exposure beyond the LAN. Nothing here opens a router port, and the certificate is not
  publicly trusted; for the internet you want a real domain, a public CA (Caddy does that
  automatically with a DNS name it can answer for) and a stronger authenticator than basic auth.
* No per-user accounts inside the apps. Everyone with the Caddy password is the same operator to
  every app; PromptCadence's approvals from the LAN are recorded as the loopback approver.
* No change to any app's code. Everything is configuration the apps already document.
