# EmailDelivery.com
[![MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/bkono/edcom-ce/blob/main/LICENSE)

EmailDelivery.com is a self-hosted email marketing platform with a built-in MTA and a user experience comparable to SaaS ESPs like Maropost, Brevo, and AWeber.

A key differentiator is **Postal Routes**: rules-based routing and load balancing across any mix of delivery backends, including ESP APIs (Amazon SES/SparkPost/Mailgun), SMTP relays, MTAs, and IP pools. 

Extensive [documentation](https://docs.emaildelivery.com) is available.

## Who this product was created for

- Those who want or need privacy, control, and ownership of their data 
- Deliverability experts
- High-volume senders
- Enterprising postmasters
- Marketing agencies
- Anyone who wants to bring email marketing in-house
- Savvy hobbyists who want to ditch their ESP because it sounds cool  

## What this product was created to address


- Most ESPs outsource email delivery to third parties such as SendGrid and arbitrage the CPM
- The typical ESP value proposition is their email marketing software, not sending infrastructure
- Commercial MTAs cost tens of thousands of dollars, but you need an ESP platform to use them
- ESP deliverability is unreliable; entire domains such as Yahoo or Outlook are often partially or entirely undeliverable 
- Dedicated IP address costs at ESPs have greatly increased, while any advantage conferred by using a dedicated IP from an ESP instead of your own is questionable
- Marketing agencies are locked into SaaS platform reseller accounts they don't own, with volume-based pricing tiers and difficult margins

## Drop the SaaS, become the SaaS

EmailDelivery.com makes you the administrator and postmaster of your own self-hosted ESP.

A single, universal, and predictable ESP user interface is decoupled from the underlying email delivery infrastructure. 

Swap delivery backends in and out as needed, without migrating all your campaigns, automations, contacts, and integrations to a new platform.

## Delivery Features

- Load-balance delivery across APIs, relays, and MTAs
- SMTP relay support works with any ESP or MTA
- API integrations with Amazon SES, SparkPost, and Mailgun
- Send using your own IP addresses with our built-in Velocity MTA
- Install Velocity MTA on unlimited servers and use unlimited IPs
- Automated IP warmup throttling
- Customize delivery for mailbox providers like Gmail, Yahoo, Outlook, and iCloud
- Partition customer accounts into separate IP pools and ESP accounts
- Automatically pause customer accounts for high bounces or complaints
- Comprehensive IP and domain-level delivery reporting
- Dashboard shows a unified view of all customer open, bounce, and complaint rates
- Automated complaint feedback loop processing
- Automated bounce processing
- Full header control, including one-click List-Unsubscribe


## ESP Features

- Campaigns, email blasts, and broadcasts
- Email sequences, autoresponders, drip campaigns, and funnels
- Transactional SMTP relay and API
- Tags
- Contact lists
- Advanced segmentation with conditional logic and subgrouping
- Personalization using any contact list property
- Pop-up forms with exit-intent detection
- Slideouts, floating bars, and embedded forms
- Native drag-and-drop email composer
- WYSIWYG email composer via TinyMCE
- Raw HTML email composer
- Embedded Beefree HTML Email Builder
- Pixabay integration
- Drag-and-drop form designer
- Automated resend to non-openers
- Comprehensive reporting on campaigns and contact lists
- Suppression and exclusion lists
- Sending throttles by minute, hour, or day
- Event webhooks
- API access
- Pabbly integration
- Zapier integration


## Platform Structure


### ESP Workspace (Customer Accounts)

Customer accounts are where the ESP software lives.

- Send email
- Manage and segment contacts
- Add pop-up forms to your website 
- Integrate with external platforms

### Delivery Orchestration (Admin Portal)

The Admin Portal is where you become the architect of your email delivery infrastructure.  

- Send over your own IP addresses with built-in Velocity MTA
- Outsource delivery to a third-party service like Amazon SES
- Load balance delivery across all sending methods and services 
- Create custom rulesets directing which sending methods (API, MTA, Relay) deliver to which mailbox provider (Gmail, Yahoo, Outlook, etc.)
- Email is sent from the ESP workspace by connecting a customer account to a delivery ruleset  

## Postal Routes

Postal Routes are at the heart of the EmailDelivery.com platform.

Postal Routes are custom email delivery rulesets that provide postmasters with unlimited flexibility to slice, dice, mix, match, chop and screw delivery across customer accounts.  

This allows email to be routed where you see the best delivery/deliverability, and to isolate customer accounts from each other to carefully manage their reputations. 

### Postal Routes in practice

| Customer Account | Mailbox Provider Domain | Sending Method        | Sending Route   |
|---|---|---|---|
| Acme | Gmail | Amazon SES | SES Account 1 |
| Acme | Yahoo | SparkPost | SP Account 3 |
| Acme | iCloud | SMTP Relay | SMTP.com Account |
| Acme | All other domains | Velocity MTA Server | IP Pool 1 |
| Cyberdyne | Gmail | PowerMTA Server | IP Pool 5 |
| Cyberdyne | Yahoo | Mailgun | MG Account 2 |
| Cyberdyne | All other domains | Amazon SES | SES Account 1 |

### Postal Routes can be selected dynamically at send-time in the ESP workspace

| Customer Account | ESP Feature | Postal Route |
|---|---|---|
| Cyberdyne | Blast campaign | Postal Route A |
| Cyberdyne | Email sequence message 1 | Postal Route X |
| Cyberdyne | Email sequence message 2 | Postal Route Y |
| Cyberdyne | Email sequence message 3 | Postal Route Z |
| Cyberdyne | Transactional mail | Postal Route B |

## Installation

For best performance, a dedicated Ubuntu VPS with 2 vCPU, 4 GB RAM, and NVMe storage is a good minimum spec. 

See the [docs](https://docs.emaildelivery.com/docs/faq/recommended-vps) for suggested providers. 

> [!IMPORTANT]
> Installation must be run as **root**. `sudo` is unsupported.

## Docker Compose is a prerequisite

> [!CAUTION]
> This optional next step will uninstall any older Docker versions.

A Docker install script for Ubuntu is included for convenience:
```bash
./install_docker_on_ubuntu.sh
```

## Create a subdomain in your DNS provider

Add an A record such as `esp.yourdomain.com` where your DNS records are hosted (e.g., Cloudflare), and point it to the IP address of your VPS or server.

You'll need to enter this domain and IP address during installation.  

## Download the installation archive and run the setup wizard:

**Intel/AMD:**
```bash
curl -LO https://github.com/bkono/edcom-ce/releases/latest/download/edcom-install-amd64.tgz
tar -xzvf edcom-install-amd64.tgz
cd edcom-install
./ez_setup.sh 
```
**ARM/AArch64:** 
```bash
curl -LO https://github.com/bkono/edcom-ce/releases/latest/download/edcom-install-arm64.tgz
tar -xzvf edcom-install-arm64.tgz
cd edcom-install
./ez_setup.sh 
```

## Common installation issues
Problem? See [this section](https://docs.emaildelivery.com/docs/common-installation-issues/dont-use-sudo) in the documentation.

## Access your ESP platform 
Open your web browser to the domain you configured above (e.g., `esp.yourdomain.com`) and log in with the admin account you created during setup. 

Follow the [getting ready to send](https://docs.emaildelivery.com/docs/introduction/getting-ready-to-send) guide to send your first message.

Secure your platform with [HTTPS](https://docs.emaildelivery.com/docs/options-for-adding-https/free-native-ssl-via-lets-encrypt).

## Beefree is embedded as an optional licensed editor 

**Bring your own Beefree license:**
1. Edit `edcom-install/config/edcom.json` to add your Beefree Client ID, Client Secret, and Content Services API key
2. Run `./restart.sh` in `edcom-install`

**EmailDelivery.com customers:**
1. Create a file called `edcom-install/config/commercial_license.key` containing your license key
2. Run `./restart.sh` in `edcom-install`


## Get started with Velocity MTA
Velocity MTA is tightly integrated with the EmailDelivery.com ESP platform, so the ESP must be installed **before** Velocity for the MTA to function.

Velocity MTA is **optional** and self-contained. Use it only when you want to send from your own IPs on a VPS or dedicated server.

Backend ESP APIs, external SMTP relays, and inbound transactional SMTP work without it.

Start by reading the [what you need to know before you get started](https://docs.emaildelivery.com/docs/what-you-need-to-know-before-you-install-velocity-mta) documentation.

Follow the [getting ready to send](https://docs.emaildelivery.com/docs/velocity-mta-basics/getting-ready-to-send) guide.

For a general background on MTAs and using your own IP addresses vs an ESP, see the [MTA FAQ](https://docs.emaildelivery.com/docs/faq/velocity-mta-faq), which covers this topic in detail. 
