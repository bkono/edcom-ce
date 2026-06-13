#!/usr/bin/env bash

set -e
set -x

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
cd "$SCRIPT_DIR/.."

PLATFORM=linux/arm64
BUILD=${BUILD:-$(date -u +1.%Y%m%d.%H%M)}

echo "VERSION = '$BUILD'" > api/shared/version.py

sudo rm -rf .build

mkdir .build
mkdir .build/edcom-install
mkdir .build/edcom-install/images
mkdir .build/velocity-install
mkdir .build/velocity-install/images

cat <<"EOM" > .build/edcom-install/load_images.sh
#!/usr/bin/env bash
set -e
docker load < images/edcom-database.tgz
docker load < images/edcom-api.tgz
docker load < images/edcom-smtprelay.tgz
docker load < images/edcom-screenshot.tgz
docker load < images/edcom-proxy.tgz
EOM
chmod +x .build/edcom-install/load_images.sh

cat <<"EOM" > .build/velocity-install/load_images.sh
#!/usr/bin/env bash
set -e
docker load < images/edcom-velocity.tgz
EOM
chmod +x .build/velocity-install/load_images.sh

mkdir .build/edcom-install/config
mkdir .build/edcom-install/config/linkcerts
echo "$BUILD" > .build/edcom-install/config/VERSION
cp config/edcom.defaults.json .build/edcom-install/config/edcom.defaults.json
cp config/nginx.server.conf .build/edcom-install/config/nginx.server.conf
cp config/nginx.ssl.server.conf .build/edcom-install/config/nginx.ssl.server.conf
cd .build/edcom-install
ln -s config/edcom.env .env
ln -s config conf
cd ../..
cp create_admin.sh .build/edcom-install
cp reset_password.sh .build/edcom-install
cp change_username.sh .build/edcom-install
cp convert_to_ssl.sh .build/edcom-install
cp update_domain.sh .build/edcom-install
cp upgrade.sh .build/edcom-install
cp restart.sh .build/edcom-install
cp ez_setup.sh .build/edcom-install
cp install_docker_on_ubuntu.sh .build/edcom-install
cp generate_letsencrypt_certificate.sh .build/edcom-install
cp renew_letsencrypt_certificate.sh .build/edcom-install
cp generate_smtp_certificate.sh .build/edcom-install
cp renew_smtp_certificate.sh .build/edcom-install
cp generate_link_certificate.sh .build/edcom-install
cp renew_link_certificate.sh .build/edcom-install
cp docker-compose.prod.yml .build/edcom-install/docker-compose.yml
cp -r schema .build/edcom-install
mkdir .build/edcom-install/data
mkdir .build/edcom-install/data/buckets
mkdir .build/edcom-install/data/buckets/data
mkdir .build/edcom-install/data/buckets/blocks
mkdir .build/edcom-install/data/buckets/images
mkdir .build/edcom-install/data/buckets/transfer
mkdir .build/edcom-install/data/logs
mkdir .build/edcom-install/data/logs/nginx
mkdir .build/edcom-install/data/logs/postgres
mkdir .build/edcom-install/data/smtpcert
mkdir -p .build/edcom-install/data/letsencrypt-challenge/.well-known/acme-challenge
cp -r data/setup .build/edcom-install/data

cat <<"EOM" > .build/edcom-install/README
EmailDelivery.com Platform README
Community Edition
---------------------------------

Enable Beefree by putting your EmailDelivery.com 
commercial license key in config/commercial_license.key
and running ./restart.sh

https://github.com/bkono/edcom-ce
https://docs.emaildelivery.com/docs/introduction/getting-ready-to-send


Icons from icomoon.io licensed under https://creativecommons.org/licenses/by-sa/4.0/


EOM

mkdir .build/velocity-install/conf
mkdir .build/velocity-install/conf/linkcerts
mkdir -p .build/velocity-install/conf/letsencrypt-challenge/.well-known/acme-challenge
mkdir .build/velocity-install/logs
mkdir .build/velocity-install/mail
echo "$BUILD" > .build/velocity-install/VERSION
cd .build/velocity-install
ln -s conf config
cd ../..
cp config/mta.defaults.conf .build/velocity-install/conf/
cp install_docker_on_ubuntu.sh .build/velocity-install
cp restart.velocity.sh .build/velocity-install/restart.sh
cp generate_link_certificate.velocity.sh .build/velocity-install/generate_link_certificate.sh
cp renew_link_certificate.velocity.sh .build/velocity-install/renew_link_certificate.sh
cp docker-compose.velocity.yml .build/velocity-install/docker-compose.yml

cat <<"EOM" > .build/velocity-install/README
EmailDelivery.com Velocity MTA README
Community Edition
---------------------------------

https://github.com/bkono/edcom-ce
https://docs.emaildelivery.com/docs/what-you-need-to-know-before-you-install-velocity-mta/the-esp-platform-ip-cant-be-used-with-the-mta
https://docs.emaildelivery.com/docs/velocity-mta-basics/getting-ready-to-send
https://docs.emaildelivery.com/docs/faq/velocity-mta-faq

EOM

docker image build . -f services/database.Dockerfile --tag edcom/database:latest --platform $PLATFORM
docker image build . -f services/api.Dockerfile --tag edcom/api:latest --platform $PLATFORM
docker image build smtprelay --tag edcom/smtprelay:latest --platform $PLATFORM
docker image build screenshot --tag edcom/screenshot:latest --platform $PLATFORM
docker image build . -f services/client-build.Dockerfile --tag edcom/client-build:latest --platform $PLATFORM
docker run --rm --platform "$PLATFORM" -v "$PWD/client/src:/client/src" -v "$PWD/client/public:/client/public" -v "$PWD/client/build:/client/build" edcom/client-build:latest
docker image build . -f services/proxy.Dockerfile --tag edcom/proxy:latest --platform $PLATFORM
docker image build velocity --tag edcom/velocity:latest --platform $PLATFORM

docker save edcom/database:latest | gzip > .build/edcom-install/images/edcom-database.tgz
docker save edcom/api:latest | gzip > .build/edcom-install/images/edcom-api.tgz
docker save edcom/smtprelay:latest | gzip > .build/edcom-install/images/edcom-smtprelay.tgz
docker save edcom/screenshot:latest | gzip > .build/edcom-install/images/edcom-screenshot.tgz
docker save edcom/proxy:latest | gzip > .build/edcom-install/images/edcom-proxy.tgz
docker save edcom/velocity:latest | gzip > .build/velocity-install/images/edcom-velocity.tgz

# Adjust compose files to the correct platform inside the package.
#sed -i 's/linux\\/amd64/linux\\/arm64/g' .build/edcom-install/docker-compose.yml
#sed -i 's/linux\\/amd64/linux\\/arm64/g' .build/velocity-install/docker-compose.yml

sed 's|linux/amd64|linux/arm64|g' .build/edcom-install/docker-compose.yml > .build/edcom-install/docker-compose.yml.tmp
mv .build/edcom-install/docker-compose.yml.tmp .build/edcom-install/docker-compose.yml

sed 's|linux/amd64|linux/arm64|g' .build/velocity-install/docker-compose.yml > .build/velocity-install/docker-compose.yml.tmp
mv .build/velocity-install/docker-compose.yml.tmp .build/velocity-install/docker-compose.yml

cat <<"EOM" > .build/edcom-install/config/ARM64-VERSION
This version of EmailDelivery.com is built for the ARM64/AArch64 architecture.
EOM
cat <<"EOM" > .build/velocity-install/ARM64-VERSION
This version of Velocity MTA is built for the ARM64/AArch64 architecture.
EOM

sed 's/Platform README/Platform README (ARM64 Architecture)/' .build/edcom-install/README > .build/edcom-install/tmp-readme
mv -f .build/edcom-install/tmp-readme .build/edcom-install/README

sed 's/Velocity MTA README/Velocity MTA README (ARM64 Architecture)/' .build/velocity-install/README > .build/velocity-install/tmp-readme
mv -f .build/velocity-install/tmp-readme .build/velocity-install/README

sudo chown -R 0:0 .build/edcom-install
sudo chown 70:70 .build/edcom-install/data/logs/postgres
sudo chown -R 0:0 .build/velocity-install

cd .build
tar -zcvf edcom-install-arm64.tgz edcom-install
tar -zcvf velocity-install-arm64.tgz velocity-install
cd ..

sudo chown -R "$(id -u):$(id -g)" .build/edcom-install
sudo chown -R "$(id -u):$(id -g)" .build/velocity-install
