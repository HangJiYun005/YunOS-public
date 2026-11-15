# YunOS v8.2-D Tailscale Private Remote Access

## Scope

This node exposes the existing YunOS mobile PWA only inside the user's private
Tailscale network:

```text
iPhone cellular data
  -> private Tailnet HTTPS / MagicDNS
  -> Tailscale Serve
  -> http://127.0.0.1:8765
  -> YunOS web-real-readonly
```

The production database remains on the Windows host. The application stays
bound to `127.0.0.1`; no router port, public reverse proxy, Funnel, exit node,
external tunnel, extra account, or application login is added.

## Local operations

Run these commands from the repository root:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_yunos_private_access.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_yunos_private_access.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_yunos_private_access.ps1
```

The start script is idempotent. It refuses to reuse port 8765 unless the
existing service identifies itself as `real-readonly`, read-only, and bound to
`127.0.0.1:8765`. The stop script performs the same identity checks and stops
only the owning YunOS Python process. It does not disconnect Tailscale and does
not alter Serve.

## Login persistence

Register the single per-user logon entry once:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_yunos_private_access_startup.ps1
```

The entry name is `YunOS Private Readonly Access` under the current user's
Windows `Run` registry key and runs with limited user privileges. The start
script verifies and reuses the expected listener instead of creating a
duplicate process. Tailscale's Windows service and Serve configuration are
maintained by Tailscale itself; the logon entry starts only YunOS.

To remove the logon entry without changing YunOS, Tailscale, or Serve:

```powershell
Remove-ItemProperty `
  -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
  -Name "YunOS Private Readonly Access"
```

## Tailscale Serve

The one-time local configuration targets only the loopback service:

```powershell
tailscale serve --bg --yes http://127.0.0.1:8765
tailscale serve status
```

Do not store the resulting private hostname in source control. Never use
`tailscale funnel` for this node.

## Safety and acceptance gates

- The production database, WAL, and SHM hashes must match their pre-test
  values.
- Remote application/API traffic must remain same-origin HTTPS and GET-only.
- CacheStorage may contain only the static application shell; search results,
  conversation bodies, attachments, and runtime data must not be cached.
- A stopped YunOS backend must produce an explicit connection/search error,
  never an empty-results success state, and a manual retry must work after
  recovery.
- Desktop v8.1 and mobile v8.2-C interaction/visual baselines remain unchanged.
- Final iPhone cellular acceptance is manual and is not claimed by automated
  Windows/Chromium evidence.
