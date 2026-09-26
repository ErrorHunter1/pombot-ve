log "Installiere nginx, MariaDB und PHP …"
apt_install nginx mariadb-server php-fpm php-mysql php-curl php-gd php-mbstring php-xml php-zip php-intl unzip
enable_now mariadb nginx "$(php_fpm)"

DBPW="$(genpw 24)"
log "Lege Datenbank an …"
mysql <<SQL
CREATE DATABASE IF NOT EXISTS wordpress DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'wordpress'@'localhost' IDENTIFIED BY '${DBPW}';
ALTER USER 'wordpress'@'localhost' IDENTIFIED BY '${DBPW}';
GRANT ALL PRIVILEGES ON wordpress.* TO 'wordpress'@'localhost';
FLUSH PRIVILEGES;
SQL

log "Lade WordPress …"
rm -rf /var/www/wordpress
curl -fsSL https://wordpress.org/latest.tar.gz | tar -xz -C /var/www
cd /var/www/wordpress
cp wp-config-sample.php wp-config.php
sed -i "s/database_name_here/wordpress/; s/username_here/wordpress/; s/password_here/${DBPW}/" wp-config.php
# Sicherheitsschlüssel erzeugen (statt der Platzhalter aus der Vorlage)
sed -i "/put your unique phrase here/d" wp-config.php
SALTS="$(mktemp)"
for key in AUTH_KEY SECURE_AUTH_KEY LOGGED_IN_KEY NONCE_KEY AUTH_SALT SECURE_AUTH_SALT LOGGED_IN_SALT NONCE_SALT; do
  printf "define( '%s', '%s' );\n" "$key" "$(genpw 64)" >> "$SALTS"
done
sed -i "/DB_COLLATE/r ${SALTS}" wp-config.php
rm -f "$SALTS"
chown -R www-data:www-data /var/www/wordpress

PHPSOCK="$(ls /run/php/php*-fpm.sock | head -n1)"
SERVER_NAME="${APP_DOMAIN:-_}"
cat > /etc/nginx/sites-available/wordpress <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name ${SERVER_NAME};
    root /var/www/wordpress;
    index index.php;
    client_max_body_size 64m;

    location / { try_files \$uri \$uri/ /index.php?\$args; }
    location ~ \.php\$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:${PHPSOCK};
    }
    location ~ /\.ht { deny all; }
}
EOF
ln -sf /etc/nginx/sites-available/wordpress /etc/nginx/sites-enabled/wordpress
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
    info "HINWEIS: Zertifikat fehlgeschlagen (zeigt die Domain per DNS auf ${IP}?). Später: certbot --nginx -d ${APP_DOMAIN}"
  fi
fi

info "WordPress: ${URL}/"
info "Beim ersten Aufruf richtest du Titel und Admin-Konto ein."
info ""
info "Datenbank: wordpress / Benutzer: wordpress / Passwort: ${DBPW}"
info "Dateien: /var/www/wordpress"
log "Fertig."
