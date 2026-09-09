# LAN access — the suite from other machines on your network, through WeightRoomGym

**Audience:** an operator who wants the suite reachable from a laptop, phone or second
workstation on the same LAN. **Reference machine:** `Jordan-main`, `10.77.10.84`, Ubuntu with
systemd and Avahi (`jordan-main.local` resolves on the LAN).

**Read first:** [ADR-0126](adr/0126-weightroom-is-the-only-service-on-the-lan-and-terminates-tls-with-its-own-ca.md)
(why one service, why its own CA, why a login), [ADR-0026](adr/0026-local-http-hardening.md) (the
Host allowlist, CSRF), [ADR-0125](adr/0125-weightroom-drives-the-applications-through-systemd-user-units-it-writes.md)
(how the applications run), each app's `docs/security.md` (its own exposure rules, unchanged).

**Status (2026-09-09, row W0):** this is the design. WeightRoomGym rows W1 (TLS, login, `setup`) and
W2 (units) build it; until they ship there is no supported LAN path — the Caddy script this
document used to describe was retired with this rewrite, and a machine that still runs its Caddy
site and units should turn them off (`sudo systemctl disable --now caddy`; `systemctl --user
disable --now freeweight loadcoach ideapress promptcadence`) before running `wr-gym setup`.

---

## 1. The shape, and why

Every application is **local-first**: bound to `127.0.0.1`, no credentials, safe only because
nothing off the machine can reach it. Each refuses to start on a non-loopback bind without a Host
allowlist and a credential (`INSECURE_BINDING`). Two facts decide the shape:

1. **The web UIs need TLS from any hostname but `localhost`.** The CSRF cookie every form uses is
   `__Host-`-prefixed and `Secure`; browsers set it over `http://localhost` and over `https://`,
   and over nothing else.
2. **IdeaPress has no authentication and PromptCadence's console is loopback-first by decision**
   ([ADR-0094](adr/0094-the-console-authenticates-as-the-api-does.md)). Something in front of
   them has to ask who you are.

So: **the four applications stay on loopback, and exactly one thing faces the LAN — WeightRoomGym.**
It terminates TLS under a certificate authority it creates, asks for a username and password,
and gives you every application's control surface as its own pages. Nothing about the
applications' posture changes: they still bind loopback, still require no token, and their own
UIs still work on the machine at `http://localhost:<port>`.

| Service | On the machine | On the LAN |
|---|---|---|
| WeightRoomGym | `https://localhost:8769` | **`https://jordan-main.local:8769`** (or `https://10.77.10.84:8769`) |
| Trust page (the root certificate, plain HTTP) | — | `http://jordan-main.local:8770/trust` |
| FreeWeight | `http://127.0.0.1:8765` | not reachable — use the FreeWeight tab in WeightRoomGym |
| LoadCoach | `http://127.0.0.1:8766` | not reachable — the LoadCoach tab |
| IdeaPress | `http://127.0.0.1:8767` | not reachable — the IdeaPress tab |
| PromptCadence | `http://127.0.0.1:8768` | not reachable — the PromptCadence tab |
| Ollama | `http://127.0.0.1:11434` | see §5 |

The composition between applications (IdeaPress → LoadCoach, PromptCadence → LoadCoach) keeps
using loopback and is untouched.

**Not chosen:** binding an application to `10.77.10.84` with `auth.tokens` / `token create`. It
works for an API caller with a bearer token — a script on the laptop calling LoadCoach directly —
still needs TLS in front for any UI, and exposes a port per application. Keep it for a headless
API-only host; [master architecture §8.2](architecture/master-architecture.md) still describes it.

---

## 2. Turning it on

```bash
pip install wr-gym            # or the workspace's install_local.sh
wr-gym setup                      # asks: bind (LAN interface or loopback), username, password
wr-gym serve                      # or: systemctl --user start weightroom  (setup enabled it)
```

What `setup` does, in order — each step is what you would do by hand:

1. Creates the certificate authority and the server certificate under
   `~/.config/wr-gym/tls/` (ECDSA P-256; the root lasts 10 years, the leaf 398 days and
   renews itself; the leaf names the hostname, `<hostname>.local`, every LAN address, and
   `localhost`).
2. Creates the operator account (the password is scrypt-hashed; the plaintext is never written).
3. Fills `server.allowed_hosts` with the hostname, `<hostname>.local` and the LAN addresses, and
   sets `server.host` to the interface you chose (or leaves loopback).
4. Creates a `write` token on LoadCoach and a `write,approve` token on PromptCadence for chat,
   stored as files under `~/.config/wr-gym/secrets/` and named by reference in
   `config.toml` — not required while the applications bind loopback, kept so chat survives an
   application moving off it.
5. Enables lingering (`loginctl enable-linger`) so user units outlive logins, writes the five
   `systemd --user` units (`weightroom`, `freeweight`, `loadcoach`, `ideapress`,
   `promptcadence`), enables and starts them, and waits for each `/api/v1/health`.
6. Prints the root certificate's fingerprint, the trust URLs, and — if you want the Ollama
   restart button — the polkit rule and the `sudo install` command for it
   ([ADR-0125](adr/0125-weightroom-drives-the-applications-through-systemd-user-units-it-writes.md)
   rule 5). Nothing in `setup` runs `sudo`.

Re-running is safe. `wr-gym units sync` regenerates the unit files; `wr-gym doctor` tells
you what is missing, including anything §2 of [`MEMORY_SAFETY.md`](MEMORY_SAFETY.md) still wants
on the host.

---

## 3. Trusting the certificate on each client

WeightRoomGym's CA is one only this machine knows. Every client device must trust its root
certificate **once**; until it does, the browser shows a certificate warning, and — worse than the
warning — treats the origin as insecure, so the `__Host-` cookies are never set and login fails
even after you click through. Trust the root; do not click through.

**Get the certificate:** `http://jordan-main.local:8770/root.crt` (or
`http://10.77.10.84:8770/root.crt`) from the client, or `~/.config/wr-gym/tls/ca.crt` from the
server. It is the CA's public certificate; it contains no secret.

**Verify before trusting** — on the server, print the fingerprint and compare it on the client:

```bash
wr-gym trust          # prints the SHA-256 fingerprint, the paths, the URLs and these steps
openssl x509 -in ~/.config/wr-gym/tls/ca.crt -noout -fingerprint -sha256 -subject
```

### Linux (Debian/Ubuntu — system store, curl, Chrome, Chromium)

```bash
sudo cp root.crt /usr/local/share/ca-certificates/weightroom-jordan-main.crt   # must end in .crt
sudo update-ca-certificates
```

Firefox keeps its own store: `Settings → Privacy & Security → Certificates → View Certificates
→ Authorities → Import…`, pick `root.crt`, tick *Trust this CA to identify websites*. (Or run
`certutil -d sql:$HOME/.mozilla/firefox/<profile> -A -t "C,," -n weightroom-jordan-main -i root.crt`
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

1. Open `http://jordan-main.local:8770/root.crt` in Safari (not Chrome) → *Allow* the profile
   download.
2. *Settings → General → VPN & Device Management → Downloaded Profile → Install* (device passcode).
3. **Then, separately:** *Settings → General → About → Certificate Trust Settings* → turn on
   *Enable Full Trust for Root Certificates* for the WeightRoomGym root. Without step 3 the profile is
   installed but not trusted for websites.

### Android

*Settings → Security & privacy → More security & privacy → Encryption & credentials → Install a
certificate → CA certificate* → *Install anyway* → pick the downloaded `root.crt`. (Menu names
vary by vendor and version; the store is always "CA certificate", not "VPN & app user
certificate".) Chrome trusts user CAs; some apps built with a strict network-security config do
not — the console is fine in Chrome and Firefox.

### The server itself

```bash
sudo cp ~/.config/wr-gym/tls/ca.crt /usr/local/share/ca-certificates/weightroom-jordan-main.crt
sudo update-ca-certificates
```

Only needed for browsing `https://jordan-main.local:8769` *from* the server; `https://localhost:8769`
also needs it (the console never serves plain HTTP), and `curl --cacert ~/.config/wr-gym/tls/ca.crt`
works without touching the system store.

### Check it worked

```bash
curl --cacert ~/.config/wr-gym/tls/ca.crt https://jordan-main.local:8769/api/v1/version   # no -k
```

A browser at `https://jordan-main.local:8769` shows a padlock, asks for the password once, and the
four applications' tabs work — forms included.

---

## 4. Operating it

* **Change the password:** `wr-gym operator password` on the server (revokes every session).
* **Logs:** the *Logs* page of any application in the console; on the server
  `journalctl --user -u weightroom -f` (and the other four).
* **Renew the certificate:** it renews itself at startup within 30 days of expiry; `wr-gym tls
  renew` forces it. No re-trust needed — the root is unchanged.
* **Rotate the CA** (a device you no longer control has the root): `wr-gym tls rotate`, then
  re-trust on every client (§3). Every session is revoked.
* **Turn the LAN off:** set `server.host = "127.0.0.1"` in `~/.config/wr-gym/config.toml`
  and restart; the console is then local-only and still HTTPS.
* **API clients on the LAN** (a script on the laptop calling LoadCoach): WeightRoomGym's API is for
  its own pages. Expose the application itself the token-and-proxy way
  ([master architecture §8.2](architecture/master-architecture.md)), or run the script on the
  server. A WeightRoomGym API token for automation is a listed future extension
  ([spec §21](apps/weightroom/spec.md)).

## 5. Ollama

The reference machine's `ollama.service` override sets `OLLAMA_HOST=0.0.0.0:11434`, so Ollama
itself listens on the LAN, unauthenticated. That is the operator's daemon and the operator's
choice — it is what a LAN client that talks to Ollama directly needs — but it is the one thing on
this machine besides WeightRoomGym that answers from another room. If no such client exists, set
`OLLAMA_HOST=127.0.0.1:11434` in the override ([`MEMORY_SAFETY.md`](MEMORY_SAFETY.md) §2.1 shows
the file) and restart Ollama; `wr-gym doctor` reports the `0.0.0.0` bind as a notice either way.

## 6. What this does not do

* No exposure beyond the LAN. Nothing here opens a router port, and the certificate is not
  publicly trusted; for the internet you want a real domain, a public CA and a stronger
  authenticator than one password ([ADR-0126](adr/0126-weightroom-is-the-only-service-on-the-lan-and-terminates-tls-with-its-own-ca.md)
  rule 10).
* No per-user accounts. One operator account in 1.0; a second person is a future record.
  PromptCadence's approvals granted from the console are recorded under WeightRoomGym's token name.
* No change to any application's code or posture. Every application keeps its `docs/security.md`
  word for word.
