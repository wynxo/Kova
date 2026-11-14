# Using Kova from your phone

Kova runs on your computer. Phone access lets a phone on the **same Wi-Fi** use
it through the phone's browser. The model, your chats, memory and files stay on
the computer, and the phone is only a window into it.

## Pair a phone

```console
$ kova pair
Turn on phone access? [Y/n]: y
█▀▀▀▀▀█ ▄▀▄ ...   (a QR code)
Scan this with your phone's camera (same Wi-Fi as this computer).
✓ Paired iPhone. Add Kova to its home screen for one-tap access.
```

The web and desktop apps have the same flow under **Connect your phone** in the
sidebar.

| Command | What it does |
| --- | --- |
| `kova pair` | Turn phone access on (asks first) and show a one-use pairing QR code |
| `kova pair --off` | Stop listening on the network. Paired phones stay paired but cannot connect |
| `kova devices` | List paired phones, when they were last seen, and whether access is on |
| `kova devices revoke ID` / `--all` | Unpair one phone, or all of them, immediately |

On the phone, the log-out button in the sidebar unpairs that phone.

## How it works

1. **Opt-in binding.** By default Kova listens only on `127.0.0.1`. Turning on
   phone access sets `bind = "lan"` in `config.toml` and starts a second
   listener on this computer's **private LAN address**, such as `192.168.1.20`.
   It never listens on `0.0.0.0`, so VPN, hotspot and public interfaces stay
   closed. The loopback listener does not change.
2. **One-use pairing code.** `kova pair` mints a random secret that lives for
   two minutes and works once. The QR code holds
   `http://<lan-ip>:<port>/app#pair=<secret>`. The secret is in the URL
   *fragment*, which browsers never send to a server or write to a log. The
   page strips it at once.
3. **Device token.** The page trades the secret for a random 256-bit device
   token in an `HttpOnly`, `SameSite=Strict` cookie that lasts about a year.
   Kova stores only its SHA-256 hash, in `devices.json` (mode 0600). Paired
   devices survive restarts, so the phone does not have to pair again each day.
4. **Stricter rules on the LAN listener.** Every request on it must:
   - come from a private, loopback or link-local address, so a port-forward
     from the internet is refused;
   - carry the exact `Host` `<lan-ip>:<port>`, which defeats DNS rebinding,
     plus the usual same-origin `Origin` and `Sec-Fetch-Site` checks;
   - carry a paired device's cookie, except for the page, its assets and the
     pairing claim.

   On that listener the CLI bearer token, desktop browser sessions and the
   routes that manage access (pairing, devices, turning phone access on or off)
   are refused. A phone cannot pair other phones or keep itself from being
   revoked.
5. **Limits.** Ten wrong pairing secrets from one address within five minutes
   get `429`. At most four codes are open at once.

## Threat model

- **Who can get in:** only a browser holding a paired device's cookie, on your
  local network. A paired phone is you: it can chat, use tools and change
  chat settings. Destructive actions still stop for your approval as usual.
- **Plain HTTP on the LAN.** Kova does not serve TLS, because a self-signed
  certificate would mean scary browser warnings. Someone who can sniff your
  Wi-Fi traffic, such as an untrusted guest network or an open hotspot, could
  copy the device cookie. Use phone access on networks you trust. If a phone is
  lost or you suspect a leak, run `kova devices revoke --all`.
- **Other devices on the Wi-Fi** can see that a Kova page exists, but nothing
  else: every API route answers `401` without a paired cookie.
- **Not covered:** reaching Kova from outside your home network. That is
  deliberately out of scope. A follow-up can document a private overlay such as
  Tailscale.

## Troubleshooting

- *"No Wi-Fi or wired network found"*: the computer has no private IPv4
  address. Connect it to your router. Set `KOVA_LAN_ADDRESS=192.168.x.y` to pick
  a specific address (it must be private).
- *The phone cannot open the page*: check that both devices are on the same
  network (guest networks often isolate clients) and that a firewall allows
  incoming connections to Kova's port (7700 by default).
- *Windows asks whether to allow Python on private networks*: allow it for
  private networks only. That is the listener for your phone.
- *The computer's IP address changed*: the phone's cookie belongs to the old
  address, so run `kova pair` and scan again, then revoke the old entry with
  `kova devices revoke`. A DHCP reservation in your router keeps the address
  stable.
