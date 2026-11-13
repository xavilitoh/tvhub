# TVHub add-on

Opens TVHub from the Home Assistant sidebar. Home Assistant signs you in, so TVHub shows no login page there and
every Home Assistant user who can open the panel is an administrator in TVHub.

## Install

1. [![Open your Home Assistant instance and show the add add-on repository dialog with this repository pre-filled.](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fxavilitoh%2Ftvhub)
   or by hand: *Settings → Add-ons → Add-on store → ⋮ → Repositories* and add `https://github.com/xavilitoh/tvhub`.
2. Install **TVHub**, start it and enable *Show in sidebar*.
3. Add your TVs (see the main README for each brand).

## Integration (media players and card)

The add-on announces itself to Home Assistant, so once the TVHub integration is installed you get a **TVHub discovered**
card under *Settings → Devices & services*; confirm it. To have the add-on install the integration for you, turn on the
option **install_integration** and restart Home Assistant (it copies it to `/config/custom_components/tvhub`; a copy
installed with HACS or by hand is never touched). The add-on mounts the Home Assistant config folder read-write for this,
but writes to it only when that option is on.

## MQTT

With the **Mosquitto broker** add-on installed, TVHub connects to it by itself on its first start. If you install Mosquitto afterwards, open *Home Assistant* in TVHub's menu and press **Usar el broker de Home Assistant**. An external broker has to be filled in by hand.

## Direct access (outside Home Assistant)

The add-on uses the host network, so TVHub also answers on `http://<home-assistant-ip>:8080` (change the port in the add-on's *Configuration* tab, option **port**, if 8080 is taken; restart the add-on afterwards). Ingress itself always uses port 8099, which is also reachable on the network but only trusts Home Assistant, so it asks for a login too. That door is **not**
covered by Home Assistant: it asks for TVHub's own login, exactly like the standalone Docker image. The first time you
open it, create the administrator with the setup code printed in the add-on log. Use that address (or an API token)
for the TVHub integration in Home Assistant and for other devices.

## Security

- The login is skipped only for connections coming from the Home Assistant Supervisor (`172.30.32.2`) through Ingress.
  Everything else needs a TVHub account or API token.
- Mutating requests from a browser must come from the same origin as Home Assistant.
