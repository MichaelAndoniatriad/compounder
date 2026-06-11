# Step-by-step: run the portfolio advisor 24/7 (laptop off)

**What this does:** a cheap always-on Linux machine runs the same scheduled jobs the repo expects (weekly check, “run due” jobs, optional price watchdog). Your **Mac can be closed**; the **VPS stays on**.

**What I cannot do for you:** create the VPS account, type your API keys, or SSH into your machine. Everything below is what **you** run once on the server.

**One path you must pick:** the folder where the code lives. Examples use `/opt/tradingagents`. If you use a different path, replace **every** `/opt/tradingagents` below with yours.

---

## Step 1 — Get a small Linux server

- Any provider is fine (DigitalOcean, Linode, Hetzner, AWS Lightsail, etc.).
- **Ubuntu 22.04 or 24.04**, **1 GB RAM** is usually enough if you only use `cron` (no heavy UI).
- Note the **IP address** and log in with SSH from your laptop:

```bash
ssh youruser@SERVER_IP
```

---

## Step 2 — Install Git and Python on the server

Paste on the server:

```bash
sudo apt update
sudo apt install -y git python3.12 python3.12-venv
```

---

## Step 3 — Put the code on the server

**Option A — you use GitHub (recommended)**

```bash
sudo mkdir -p /opt && sudo chown "$USER":"$USER" /opt
cd /opt
git clone https://github.com/MichaelAndoniatriad/compounder.git tradingagents
cd tradingagents
```

(If you use your own fork, replace the URL with your fork’s clone URL.)

**Option B — you only have a zip on your laptop**

From your **laptop**, copy the project folder to the server (example):

```bash
scp -r /path/to/TradingAgents-main youruser@SERVER_IP:/opt/tradingagents
```

Then on the server:

```bash
cd /opt/tradingagents
```

---

## Step 4 — Create the Python environment and install the package

On the server, inside the repo folder:

```bash
cd /opt/tradingagents
sh scripts/setup-venv.sh python3.12
chmod +x scripts/cron-*.sh
```

Wait until it finishes without errors.

---

## Step 5 — Create `.env` with your keys

On the server:

```bash
cd /opt/tradingagents
nano .env
```

Copy the **same variables** you already use on your laptop (eToro keys, LLM keys, webhook/email if you use alerts). Save the file, then:

```bash
chmod 600 .env
```

**Important:** the user that will run `cron` (usually **your** login user) must own this file and the repo. Data and logs go under **`/home/youruser/.tradingagents/`** by default.

---

## Step 6 — Test one job by hand (proves it works)

Still on the server:

```bash
cd /opt/tradingagents
set -a && source .env && set +a
export PYTHONPATH="/opt/tradingagents${PYTHONPATH:+:$PYTHONPATH}"
./scripts/cron-portfolio-advisor-due.sh
```

Then:

```bash
tail -n 50 ~/.tradingagents/logs/portfolio-advisor-due.log
```

You should see new lines with timestamps. If you see Python errors, fix `.env` or missing packages before going to Step 7.

---

## Step 7 — Install the schedule (`crontab`)

```bash
crontab -e
```

If the editor asks, pick **nano**. Paste this block, then **change `/opt/tradingagents`** if your folder is different:

```cron
SHELL=/bin/bash
PATH=/usr/local/bin:/usr/bin:/bin

*/15 * * * * /opt/tradingagents/scripts/cron-portfolio-advisor-due.sh
0 9 * * SAT /opt/tradingagents/scripts/cron-portfolio-advisor-weekly.sh
0 10 1 * * /opt/tradingagents/scripts/cron-portfolio-advisor-replan.sh
*/5 * * * 1-5 /opt/tradingagents/scripts/cron-portfolio-advisor-watchdog.sh
```

Save and exit.

**What this means in plain language**

| Line | Meaning |
|------|--------|
| First line | Every **15 minutes**, run jobs that are due. |
| Second line | Every **Saturday 9:00** (server clock), weekly light check. |
| Third line | **1st of each month** at **10:00**, full replan (uses LLM tokens). |
| Fourth line | **Weekdays every 5 minutes**, price watchdog (it does nothing outside US equity hours unless you force it in code/config). |

Cron uses the **server’s timezone**. If the server is UTC, “9:00 Saturday” is UTC.

---

## Step 8 — Confirm cron is installed

```bash
crontab -l
```

You should see the four lines you pasted.

---

## Step 9 — Optional: web dashboard (Streamlit)

You do **not** need this for scheduled jobs. Only if you want the browser UI on the server.

### One-off (manual)

```bash
cd /opt/tradingagents
set -a && source .env && set +a
export PYTHONPATH=/opt/tradingagents
"$HOME/miniconda3/envs/ta/bin/streamlit" run ui/streamlit_app.py \
  --server.address 0.0.0.0 --server.port 8501 --browser.gatherUsageStats=false
```

Then open `http://SERVER_IP:8501` if your cloud **NSG/firewall** allows **TCP 8501**.

For a safer default, bind Streamlit to **127.0.0.1** and use an **SSH tunnel** from your laptop:

```bash
ssh -L 8501:127.0.0.1:8501 youruser@SERVER_IP
```

Browser: `http://127.0.0.1:8501`.

### Always on (systemd user service)

Use `scripts/run-streamlit-headless.sh` and `deploy/streamlit-user.service.example` from this repo.

On the server (paths assume `/opt/tradingagents` and conda env **`ta`**):

```bash
chmod +x /opt/tradingagents/scripts/run-streamlit-headless.sh
mkdir -p ~/.config/systemd/user
cp /opt/tradingagents/deploy/streamlit-user.service.example ~/.config/systemd/user/streamlit-tradingagents.service
# Edit the unit file if your install path differs.
systemctl --user daemon-reload
systemctl --user enable --now streamlit-tradingagents.service
systemctl --user status streamlit-tradingagents.service
```

So the UI keeps running after you **disconnect SSH**, enable **lingering** for your user (once, needs sudo):

```bash
sudo loginctl enable-linger "$USER"
```

Logs:

```bash
journalctl --user -u streamlit-tradingagents.service -f
```

To listen on all interfaces (public IP), add under `[Service]` in the unit file:

```ini
Environment=STREAMLIT_SERVER_ADDRESS=0.0.0.0
```

…and open **8501** in the cloud security group (prefer **your IP**/32).

---

## Step 10 — When you update the code

On the server:

```bash
cd /opt/tradingagents
git pull
"$HOME/miniconda3/envs/ta/bin/pip" install -e .
systemctl --user restart streamlit-tradingagents.service   # if you use Step 9 systemd UI
```

---

## If something fails

1. Read the log file named in the script comment (defaults under `~/.tradingagents/logs/`).
2. Run the same script **manually** like in Step 6 and read the error in the terminal.
3. Confirm `crontab -l` paths match `ls /opt/tradingagents/scripts/cron-portfolio-advisor-due.sh` exactly.

---

## Copy-paste file

See `deploy/crontab.example` for the same schedule with comments.
