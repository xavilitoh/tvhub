# TVHub (dev)

Test build from the `dev` branch: unstable, may break, and has its own data (it does not share TVs or users with the
stable TVHub add-on). It uses port 8081 for direct access and 8098 for Ingress, so both can run together.

Enable *Advanced mode* in your Home Assistant profile to see it in the store. After a new push to `dev`, wait for the
**Docker image** workflow to finish, then update: uninstall and install the add-on again if Home Assistant does not
pick up the new image.
