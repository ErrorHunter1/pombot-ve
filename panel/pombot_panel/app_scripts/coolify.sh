log "Installiere Coolify (offizielles Installationsskript) …"
curl -fsSL https://cdn.coollabs.io/coolify/install.sh -o /root/coolify-install.sh
bash /root/coolify-install.sh
info "Coolify: http://${HOST}:8000"
info "Beim ersten Aufruf legst du das Admin-Konto an – am besten sofort, bevor es jemand anderes tut."
log "Fertig."
