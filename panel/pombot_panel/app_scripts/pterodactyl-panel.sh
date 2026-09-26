# Pterodactyl Panel nach der offiziellen Anleitung (pterodactyl.io/panel/1.0/getting_started.html),
# aber ohne Rückfragen. PHP kommt aus den Paketquellen der Distribution (8.1–8.3 werden unterstützt).
log "Installiere nginx, MariaDB, Redis und PHP …"
apt_install nginx mariadb-server redis-server tar unzip git cron \
  php-cli php-fpm php-common php-gd php-mysql php-mbstring php-bcmath php-xml php-curl php-zip php-intl
enable_now mariadb redis-server nginx cron "$(php_fpm)"

log "Installiere Composer …"
curl -fsSL https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

log "Lade Pterodactyl Panel …"
mkdir -p /var/www/pterodactyl
cd /var/www/pterodactyl
curl -fsSL -o panel.tar.gz https://github.com/pterodactyl/panel/releases/latest/download/panel.tar.gz
tar -xzf panel.tar.gz
rm -f panel.tar.gz
chmod -R 755 storage/* bootstrap/cache/

DBPW="$(genpw 24)"
log "Lege Datenbank an …"
mysql <<SQL
CREATE DATABASE IF NOT EXISTS panel;
CREATE USER IF NOT EXISTS 'pterodactyl'@'127.0.0.1' IDENTIFIED BY '${DBPW}';
ALTER USER 'pterodactyl'@'127.0.0.1' IDENTIFIED BY '${DBPW}';
GRANT ALL PRIVILEGES ON panel.* TO 'pterodactyl'@'127.0.0.1' WITH GRANT OPTION;
FLUSH PRIVILEGES;
SQL

log "Webserver einrichten …"
PHPSOCK="$(ls /run/php/php*-fpm.sock | head -n1)"
SERVER_NAME="${APP_DOMAIN:-_}"
cat > /etc/nginx/sites-available/pterodactyl.conf <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_NAME};
    root /var/www/pterodactyl/public;
    index index.php;
    client_max_body_size 100m;
    client_body_timeout 120s;
    sendfile off;

    location / { try_files \$uri \$uri/ /index.php?\$query_string; }
    location ~ \.php\$ {
        fastcgi_split_path_info ^(.+\.php)(/.+)\$;
        fastcgi_pass unix:${PHPSOCK};
        fastcgi_index index.php;
        include fastcgi_params;
        fastcgi_param PHP_VALUE "upload_max_filesize = 100M \n post_max_size=100M";
        fastcgi_param SCRIPT_FILENAME \$document_root\$fastcgi_script_name;
        fastcgi_param HTTP_PROXY "";
        fastcgi_intercept_errors off;
        fastcgi_buffer_size 16k;
        fastcgi_buffers 4 16k;
        fastcgi_connect_timeout 300;
        fastcgi_send_timeout 300;
        fastcgi_read_timeout 300;
    }
    location ~ /\.ht { deny all; }
}
EOF
ln -sf /etc/nginx/sites-available/pterodactyl.conf /etc/nginx/sites-enabled/pterodactyl.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

URL="http://${HOST}"
if [ "${APP_SSL:-0}" = "1" ] && [ -n "${APP_DOMAIN:-}" ] && [ -n "${APP_EMAIL:-}" ]; then
  log "Hole Let's-Encrypt-Zertifikat für ${APP_DOMAIN} …"
  apt_install certbot python3-certbot-nginx
  if certbot --nginx -d "${APP_DOMAIN}" -m "${APP_EMAIL}" --agree-tos --non-interactive --redirect; then
    URL="https://${APP_DOMAIN}"
  else
    info "HINWEIS: Zertifikat fehlgeschlagen (zeigt die Domain per DNS auf ${IP}?) – Panel läuft über http."
  fi
fi

log "Installiere PHP-Abhängigkeiten (Composer) …"
[ -f .env ] || cp .env.example .env
COMPOSER_ALLOW_SUPERUSER=1 composer install --no-dev --optimize-autoloader --no-interaction

log "Konfiguriere Panel …"
php artisan key:generate --force
php artisan p:environment:setup --author="${APP_EMAIL}" --url="${URL}" --timezone="${APP_TZ:-Europe/Berlin}" \
  --cache=redis --session=redis --queue=redis --redis-host=127.0.0.1 --redis-pass=null --redis-port=6379 \
  --settings-ui=true --telemetry=false --no-interaction
php artisan p:environment:database --host=127.0.0.1 --port=3306 --database=panel --username=pterodactyl \
  --password="${DBPW}" --no-interaction
log "Lege Datenbanktabellen an (dauert etwas) …"
php artisan migrate --seed --force

ADMINPW="$(genpw 14)Aa1"
php artisan p:user:make --email="${APP_EMAIL}" --username="${APP_ADMIN:-admin}" --name-first=Admin --name-last=User \
  --password="${ADMINPW}" --admin=1 --no-interaction
chown -R www-data:www-data /var/www/pterodactyl

log "Warteschlange und Zeitplan einrichten …"
{ crontab -l 2>/dev/null | grep -v 'pterodactyl/artisan schedule:run' || true
  echo '* * * * * php /var/www/pterodactyl/artisan schedule:run >> /dev/null 2>&1'; } | crontab -
crontab -l | grep -q 'artisan schedule:run' || { echo "Cronjob fehlt"; exit 1; }
cat > /etc/systemd/system/pteroq.service <<'EOF'
[Unit]
Description=Pterodactyl Queue Worker
After=redis-server.service

[Service]
User=www-data
Group=www-data
Restart=always
ExecStart=/usr/bin/php /var/www/pterodactyl/artisan queue:work --queue=high,standard,low --sleep=3 --tries=3
StartLimitInterval=180
StartLimitBurst=30
RestartSec=5s

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
enable_now pteroq.service

info "Pterodactyl Panel: ${URL}"
info "Admin: ${APP_ADMIN:-admin} (${APP_EMAIL}) / Passwort: ${ADMINPW}"
info ""
info "Datenbank: panel / Benutzer: pterodactyl / Passwort: ${DBPW}"
info "Nächster Schritt: im Panel unter Admin → Nodes einen Node anlegen und dort Wings installieren"
info "(in PomBot als eigener Server mit der Anwendung „Pterodactyl Wings“)."
log "Fertig."
