# How to run this app on an M1 Mac (for testers)

This guide is for someone who has **never used developer tools**, and has **never opened Terminal**. Follow it in order. Do not skip steps.

**Time:** about 45–90 minutes the first time (downloads are large).  
**Machine:** a Mac with an **M1** chip (Apple silicon).  
**Goal:** open the trading analysis app in Safari so you can click around and test it.

You will install two programs:

- **Visual Studio Code** (often called VS Code) — used to download the project and switch to the correct version
- **Podman Desktop** — used to run the app in the background (like a small private computer inside your Mac)

You do **not** need to understand what these tools do. You only need to click where this guide says, and copy-paste a few commands into Terminal.

---

## Before you start

Ask the person who sent you this guide to:

1. Invite your **GitHub** email/account to the project `local_trading_bot_v2`
2. Confirm you should use the **`dev`** branch

You also need:

- A **GitHub account** (free). Create one at [https://github.com/signup](https://github.com/signup) if you do not have one
- A **good internet connection**
- About **10 GB** of free disk space
- Your Mac plugged in (first setup is slow)

**Where to keep the project:**  
Save it in **Documents**, not on the Desktop if your Desktop is synced with iCloud. iCloud folders can break the app.

After you download the project in Part 4, its folder will be:

```text
Documents → local_trading_bot_v2
```

On the computer that folder is named:

```text
~/Documents/local_trading_bot_v2
```

Remember that path. Almost every Terminal command after Part 4 must be run **from inside that folder**.

---

## Words used in this guide

| This guide says | What it means |
|---|---|
| Return | The big key also labeled Enter |
| Terminal | The Mac app where you paste short text commands. This is **not** Safari and **not** VS Code |
| Folder | A location on your Mac, like Documents. Terminal is always “sitting” in one folder |
| Branch | A named version of the project. You need **dev** |
| Container | A background program Podman runs for you |
| Prompt | The last line in Terminal, waiting for you to type. It often ends with `%` |

---

# How to use Terminal (read this once)

You will use Terminal several times. You do **not** type commands into Safari, Notes, or the VS Code editor. You type them only in **Terminal**.

## Open Terminal

1. Press **Command (⌘) + Space** (this opens Spotlight search)
2. Type: `Terminal`
3. Press **Return**
4. A new window opens. The background is usually black or white, with a few lines of text. That window **is** Terminal.

The last line looks something like this (names will be different):

```text
alex@Mac ~ %
```

That line is the **prompt**. The blinking cursor after `%` is where your command will appear.

The `~` means Terminal is currently in your **home folder**. That is normal the first time you open it. Your home folder is **not** the project folder.

## Paste a command (do not type it by hand)

1. Click once inside the Terminal window so it is active
2. Copy the command from this guide (the line inside the gray box)
3. Paste with **Command (⌘) + V**
4. Press **Return** to run it
5. Wait until the prompt (`%`) appears again before pasting the next command

Paste **one command at a time**. Do not paste several boxes together.

If a popup appears (for example “Install”), that is expected. Follow the on-screen buttons, then go back to Terminal.

## See which folder Terminal is in

This command can be run from **any** folder.

**Run in:** Terminal  
**Folder:** any folder is fine

```bash
pwd
```

Press **Return**. Terminal prints a path, for example:

```text
/Users/alex
```

That means you are in your home folder, **not** in the project yet.

## Go to the project folder

Do this **every time** you open a new Terminal window, before running project commands.

**Run in:** Terminal  
**Folder:** you can start from anywhere (including the home folder)

```bash
cd ~/Documents/local_trading_bot_v2
```

Press **Return**. Then check:

**Run in:** Terminal  
**Folder:** you should now be in the project (run this to confirm)

```bash
pwd
```

You should see a line that **ends with**:

```text
/Documents/local_trading_bot_v2
```

Example of a correct result:

```text
/Users/alex/Documents/local_trading_bot_v2
```

If you see `No such file or directory`, the project is not in Documents yet. Finish Part 4 first, then try `cd` again.

## Command labels in this guide

Every command below is marked like this:

- **Run in:** Terminal (always the Mac Terminal app)
- **Folder:** either **any folder** or **the project folder** `~/Documents/local_trading_bot_v2`

If it says **the project folder**, you must run `cd ~/Documents/local_trading_bot_v2` first, unless `pwd` already shows that path.

---

# Part 1 — Install Visual Studio Code

No Terminal commands in this part. Use Safari and Finder only.

## 1. Download the Apple silicon version

1. Open Safari
2. Go to: [https://code.visualstudio.com/Download](https://code.visualstudio.com/Download)
3. Click the download for **Mac** / **Apple silicon**  
   Do **not** pick Intel or “Universal” if Apple silicon is listed separately.
4. Wait for the download to finish

## 2. Put VS Code in Applications

1. Open **Finder**
2. Click **Downloads** in the left sidebar
3. Double-click the VS Code file (it ends in `.zip` or `.dmg`)
4. Drag **Visual Studio Code** into the **Applications** folder

## 3. Open it the first time

1. Open **Launchpad**, or Finder → **Applications**
2. Click **Visual Studio Code**
3. If macOS says the app is from the internet, click **Open**
4. If a “Welcome” screen appears, you can close it. You do not need an account for Microsoft.

If macOS **blocks** the app:

1. Open **System Settings** → **Privacy & Security**
2. Scroll down and click **Open Anyway** next to Visual Studio Code
3. Confirm **Open**

---

# Part 2 — Install Apple’s extra tools (one time)

Your Mac needs two small tools that Apple does not install by default: **Git** (downloads the project) and **Make** (starts the app).

## 1. Open Terminal

Follow **How to use Terminal** above if you have not opened it yet.

## 2. Install the tools

**Run in:** Terminal  
**Folder:** any folder is fine (home folder is OK — you do **not** need the project yet)

Copy this line **exactly**:

```bash
xcode-select --install
```

Paste it into Terminal (Command + V) and press **Return**.

Then:

1. A popup appears. Click **Install**
2. Agree to the license if asked
3. Wait until it finishes. This can take 10–20 minutes.
4. When it is done, the popup goes away. You can leave Terminal open.

If Terminal says the tools are **already installed**, that is fine. Continue.

---

# Part 3 — Install Podman Desktop

No Terminal commands in this part. Use Safari, Finder, and the Podman Desktop app.

This is what actually **runs** the app. Use the official installer, not Homebrew.

## 1. Download

1. Open Safari
2. Go to: [https://podman-desktop.io/downloads/macos](https://podman-desktop.io/downloads/macos)
3. Download **Apple silicon** if you see that option  
   If you only see **Universal**, that is also OK on an M1 Mac
4. Wait for the `.dmg` file to finish downloading

## 2. Install

1. Open **Downloads** in Finder
2. Double-click the Podman Desktop `.dmg` file
3. Drag **Podman Desktop** into **Applications**
4. Eject the Podman disk in Finder’s left sidebar (the small eject icon) if you see it

## 3. Open Podman Desktop and finish setup

1. Open **Podman Desktop** from Applications or Launchpad
2. If macOS asks, click **Open**
3. When you see **Get started with Podman Desktop**, click **Start Onboarding**
4. Click **Next**
5. When asked to install Podman, click **Yes**
6. Enter your **Mac password** when macOS asks (this is normal)
7. Click **Install Software**, then **Close** when it finishes
8. When asked to create a **Podman machine**, click **Next**, then **Create**  
   Wait until it says the machine is ready. This can take several minutes.
9. When asked to install extra tools:
   - **Install Compose** — yes, you need this
   - **kubectl** — you can skip this. You do not need it for this app
10. Click **Next** until you reach the Dashboard

## 4. Check that Podman is running

In Podman Desktop:

1. Click **Settings** (gear icon)
2. Open **Resources**
3. The Podman machine should show as **running**
4. Open **Settings → Docker Compatibility**
5. If you see **Podman Compose CLI Support**, make sure it is set up (use **Setup** if offered)
6. On a Mac, **Third-Party Docker Tool Compatibility** should already be on. Leave it on.

Leave Podman Desktop open. You can minimize it.

---

# Part 4 — Download the project in VS Code

No Terminal commands in this part. Use Visual Studio Code (and Safari if GitHub asks you to sign in).

## 1. Sign in to GitHub (if VS Code asks)

You will be asked to sign in when you clone a private project. Use the **same GitHub account** that was invited to the repo.

If a browser window opens:

1. Click **Authorize** / **Continue**
2. Switch back to VS Code when it says you can

## 2. Clone the project

1. Open **Visual Studio Code**
2. Press **Command (⌘) + Shift + P**
3. Type: `Git: Clone`
4. Click **Git: Clone**
5. Paste this address (this is **not** a Terminal command — paste it into the VS Code box):

```text
https://github.com/rares221/local_trading_bot_v2.git
```

6. Press **Return**
7. In the folder picker, click **Documents** (recommended), then click **Select** / **Open**
8. Wait until the download finishes
9. When VS Code asks **Would you like to open the cloned repository?**, click **Open**

You should now see a list of files on the left, including `README.md` and `docker-compose.yml`.

The project folder on your Mac is now:

```text
~/Documents/local_trading_bot_v2
```

If clone fails with **permission** or **not found**:

- You are not signed in, or
- Your GitHub account has not been invited yet

Stop and message the person who sent this guide.

---

# Part 5 — Switch to the `dev` branch

The project has several versions. Testers must use **dev**.

## Option A — click the branch name in VS Code (no Terminal)

1. Look at the **bottom-left corner** of VS Code
2. You will see a branch name, often `master`
3. Click that name
4. In the list that opens, type: `dev`
5. Click **dev** (or **origin/dev**)
6. If it asks to **checkout**, confirm

The bottom-left corner should now say **dev**.

If Option A worked, skip Option B and go to Part 6.

## Option B — use Terminal

Use this if clicking in VS Code did not work.

1. Open **Terminal** (Command + Space, type `Terminal`, press Return)
2. Go to the project folder first.

**Run in:** Terminal  
**Folder:** start from anywhere, this command *takes you* to the project

```bash
cd ~/Documents/local_trading_bot_v2
```

3. Confirm you are in the right folder.

**Run in:** Terminal  
**Folder:** should already be the project after the `cd` above

```bash
pwd
```

The printed path must end with `/Documents/local_trading_bot_v2`. If it does not, run the `cd` command again.

4. Run these **three** commands, one at a time. Wait for the `%` prompt after each one.

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
git fetch origin
```

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
git checkout dev
```

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
git pull origin dev
```

You should see a message that you are **on branch `dev`**, or that it is **Already up to date**.

If it says `dev` does not exist, message the person who sent this guide.

---

# Part 6 — Create the settings file

The app needs a small file named `.env`. You only create it once.

1. Open **Terminal**
2. If this is a new Terminal window, go to the project first.

**Run in:** Terminal  
**Folder:** start from anywhere, this command *takes you* to the project

```bash
cd ~/Documents/local_trading_bot_v2
```

3. Confirm:

**Run in:** Terminal  
**Folder:** should already be the project after the `cd` above

```bash
pwd
```

The path must end with `/Documents/local_trading_bot_v2`.

4. Create the settings file.

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`  
(If you run this from the home folder, it will fail or create the file in the wrong place.)

```bash
cp .env.example .env
```

Nothing dramatic happens. That is correct. You just copied the example settings.

You do **not** need to add API keys to test the main screens. Leave the file as it is.

---

# Part 7 — Start the app

Keep **Podman Desktop** open, with the Podman machine **running**.

## 1. Build and start (first time is slow)

1. Open **Terminal**
2. Go to the project folder if you are not already there.

**Run in:** Terminal  
**Folder:** start from anywhere, this command *takes you* to the project

```bash
cd ~/Documents/local_trading_bot_v2
```

3. Confirm with `pwd` that the path ends with `/Documents/local_trading_bot_v2`.
4. Start the app.

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
make build start
```

What to expect:

- The first run can take **10–20 minutes**
- Text will scroll. That is normal
- Your Mac fan may spin up. That is normal
- Do not close the laptop, Terminal, or Podman Desktop until it finishes
- You are done with this command when the `%` prompt comes back, with no red error at the end

If Terminal says `make: command not found`, go back to Part 2 and run `xcode-select --install` again (from **any** folder), then **quit Terminal fully** (Terminal menu → Quit Terminal) and open it again. Then `cd` to the project and retry `make build start`.

If Terminal says `docker-compose: No such file or directory` or `command not found`, stay in the **same project folder** and use these two commands instead, one after the other:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
podman compose build
```

Wait until the `%` prompt comes back, then:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
podman compose up -d --remove-orphans
```

## 2. Confirm it is running

In the **Podman Desktop** app (not Terminal), open **Containers**. You should see two items:

- `local-trading-bot`
- `trading-timescaledb`

Both should look **running** / green.

Optional check in Terminal:

**Run in:** Terminal  
**Folder:** any folder is fine

```bash
podman ps
```

You should see those two names in the list.

---

# Part 8 — Open the app in the browser

No Terminal commands in this part. Use Safari.

1. Open **Safari** (or Chrome)
2. In the address bar, type:

```text
http://localhost:6001/dashboard
```

3. Press **Return**

You should see the app **Dashboard**.

Useful pages for testing:

| Page | Address |
|---|---|
| Dashboard | http://localhost:6001/dashboard |
| Recommendations | http://localhost:6001/recommendations |
| Active trades | http://localhost:6001/active-trades |
| Journal | http://localhost:6001/journal |
| Backtest | http://localhost:6001/backtest |
| Live trading | http://localhost:6001/live-trading |
| Trade builder | http://localhost:6001/trade-builder |
| Research | http://localhost:6001/research |
| Market analysis | http://localhost:6001/analysis |
| Calendar | http://localhost:6001/calendar |
| Briefing | http://localhost:6001/briefing |

The first time the app starts, some pages may look empty for a few minutes while it loads market data. Wait, then refresh the page.

---

# Part 9 — Everyday use after the first setup

You do **not** repeat the installs. Next time:

1. Open **Podman Desktop** and wait until the machine is **running**
2. Open **Terminal** (Command + Space, type `Terminal`, press Return)
3. Go to the project folder (do this every time you open a new Terminal window):

**Run in:** Terminal  
**Folder:** start from anywhere

```bash
cd ~/Documents/local_trading_bot_v2
```

4. Confirm with `pwd` that the path ends with `/Documents/local_trading_bot_v2`.
5. Download the latest `dev` version:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
git pull origin dev
```

6. Start the app:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
make start
```

7. Open Safari at http://localhost:6001/dashboard

If the person who sent this guide says they changed the code, stay in the same project folder and run:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
make build start
```

## Stop the app when you are done

Open Terminal, go to the project folder if needed, then run **one** of these:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
make stop
```

If `make stop` fails, use this instead, still from the project folder:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
podman compose down
```

You can then quit Terminal and Podman Desktop.

---

# If something goes wrong

Try these in order. After each fix, refresh Safari.

### You are not sure which folder Terminal is in

**Run in:** Terminal  
**Folder:** any folder is fine

```bash
pwd
```

If the path does **not** end with `/Documents/local_trading_bot_v2`, run:

```bash
cd ~/Documents/local_trading_bot_v2
```

Then `pwd` again.

### The website says “can’t connect” / Safari cannot open the page

- Podman Desktop must be open and the Podman machine **running**
- Wait 1–2 minutes after `make start`, then try again
- Confirm both containers are running in Podman Desktop
- Make sure you typed `http://localhost:6001/dashboard` (not `https`)

### VS Code bottom-left does not say `dev`

Repeat Part 5. Do not test on `master` unless you were told to.

### `permission denied` or GitHub login loop

Your GitHub account needs access. Ask the project owner to invite you, then try **Git: Clone** again in VS Code.

### `make: docker-compose: No such file or directory`

Podman Compose is missing. In Podman Desktop:

1. Settings → Docker Compatibility
2. Set up **Compose**

Then in Terminal, from the **project folder**:

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
cd ~/Documents/local_trading_bot_v2
```

```bash
podman compose build
```

```bash
podman compose up -d --remove-orphans
```

### Port 6001 is already in use

Something else is using that address. Restart the Mac, open Podman Desktop, open Terminal, `cd` to the project folder, and run `make start` again.

### Very slow, or it seems stuck on first start

The first download of database and Python images is large. Leave it running. If it is still going after 30 minutes with no new text, copy the last 20 lines from the Terminal window and send them to the person who asked you to test.

### See what the app is doing (optional)

**Run in:** Terminal  
**Folder:** any folder is fine

```bash
podman logs -f local-trading-bot
```

Text will scroll. To stop watching logs, press **Control + C**. That only stops the log view, not the app.

### You changed files by accident

**Run in:** Terminal  
**Folder:** the project folder `~/Documents/local_trading_bot_v2`

```bash
cd ~/Documents/local_trading_bot_v2
```

```bash
git checkout -- .
```

```bash
git pull origin dev
```

Do **not** click **Commit** or **Sync** in VS Code unless someone asked you to. Testers only need to run the app.

---

# Command cheat sheet

Use this after the first setup. Always open **Terminal** first.

| What you want | Folder you must be in | Command |
|---|---|---|
| See current folder | any | `pwd` |
| Go to the project | any (this *moves* you) | `cd ~/Documents/local_trading_bot_v2` |
| Install Apple tools (once) | any | `xcode-select --install` |
| Switch to `dev` | project folder | `git checkout dev` |
| Get latest code | project folder | `git pull origin dev` |
| Create `.env` (once) | project folder | `cp .env.example .env` |
| First start / after code changes | project folder | `make build start` |
| Start next time | project folder | `make start` |
| Stop the app | project folder | `make stop` |
| See running containers | any | `podman ps` |

---

# Quick checklist

Use this after you finish, or if you get lost:

- [ ] VS Code is installed (Apple silicon)
- [ ] In **Terminal**, from **any folder**, `xcode-select --install` was run once
- [ ] Podman Desktop is installed, machine is **running**, Compose is installed
- [ ] Project folder is `~/Documents/local_trading_bot_v2`
- [ ] You are on the **dev** branch
- [ ] In **Terminal**, from the **project folder**, you ran `cp .env.example .env`
- [ ] In **Terminal**, from the **project folder**, `make build start` (or `podman compose up -d`) finished
- [ ] Safari opens http://localhost:6001/dashboard

When that last box is checked, you are ready to test.
