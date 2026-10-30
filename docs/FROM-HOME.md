# Using Benchtop from home

For labmates who want to use the lab's Benchtop from home. The last part is for the Benchtop admin, who sets
it up once. Other ways to share Benchtop are in [SERVER.md](SERVER.md).

## What this is

You can use the lab's Benchtop from home through **Tailscale**, a free app that joins your laptop or phone to a private network with the lab computer. Benchtop is not on the public internet: only people the lab invites can reach it, and you still sign in with your own Benchtop account.

You set it up once (about 5 minutes), then it works like being in the lab.

## Set it up once

1. **Wait for the invite.** The Benchtop admin invites you to the lab's Tailscale network; you get an email from Tailscale.
2. **Install Tailscale** from [tailscale.com/download](https://tailscale.com/download) on your laptop (Mac, Windows or Linux). For your phone, get it from the App Store or Google Play.
3. **Accept the invite** from the email, then sign in to the Tailscale app with the same account the invite went to.
4. **Turn Tailscale on.** On a Mac or Windows it sits in the menu bar or system tray; make sure it says *Connected*.
5. **Save the Benchtop address** the admin sends you as a bookmark. It looks like `https://lab-mac.tail1234.ts.net`.

You don't need a new Benchtop account: use the one you have in the lab.

## Each time you work from home

1. Check Tailscale is on (*Connected*).
2. Open your Benchtop bookmark in your browser.
3. Sign in with your usual Benchtop username and password.

Everything works as in the lab: writing up, spectra, signing and witnessing. Signing still asks for your password.

**On your phone:** turn on the Tailscale app, then open the same address. Pressing **P** on an experiment also gives a QR code that opens it on your phone; the phone needs Tailscale on for that, in the lab too.

## If it doesn't load

- **Is Tailscale on?** Open the app and check it says *Connected*. Turn it off and on again if needed.
- **Are you signed in to the right Tailscale account?** It must be the one the invite went to.
- **Is the lab computer on and Benchtop running?** If the lab Mac restarted or someone closed Benchtop, nobody can reach it until it's started again. Ask the admin.
- **Wrong password, or locked out?** Ask the Benchtop admin to reset it.

Still stuck: send the admin a screenshot of what you see.

Anything you typed that couldn't be saved (Wi-Fi dropped, Benchtop stopped) stays in your browser. Open the record again once it works and Benchtop offers to put it back.

## Keep it safe

- Don't forward your Tailscale invite or share the Benchtop address with people outside the lab. Ask the admin to invite them instead.
- Use Tailscale only on your own devices. On a shared or public computer, sign out of Benchtop when you're done.
- If you lose a laptop or phone that has Tailscale, tell the admin so they can remove it.

## For the Benchtop admin: setting up the lab Mac

Do this once, about 20 minutes. Check first with your PI or IT that reaching lab data this way is fine under the university's rules.

1. **Install Tailscale on the lab Mac** from [tailscale.com/download](https://tailscale.com/download) and sign in. This account runs the lab's private network. In the app's settings, have it open at login.
2. **Turn on names and HTTPS** in the Tailscale admin console ([login.tailscale.com/admin](https://login.tailscale.com/admin)), under **DNS**: switch on *MagicDNS* and *HTTPS certificates*.
3. **Keep the Mac awake.** System Settings → Energy (*Energy Saver* on macOS 13): turn on *Prevent automatic sleeping when the display is off* and *Start up automatically after a power failure*.
4. **Start Benchtop for the lab** in Terminal, and leave that window open:
    ```
    cd ~/benchtop
    ./benchtop lab
    ```
5. **Give it an HTTPS address.** In a second Terminal window:
    ```
    tailscale serve --bg localhost:8000
    ```
    If Terminal says `command not found`, use `/Applications/Tailscale.app/Contents/MacOS/Tailscale` in place of `tailscale`. It prints the address, like `https://lab-mac.tail1234.ts.net`, and keeps serving after restarts. `tailscale serve reset` turns it off.
6. **Make phone QR codes and email links use that address.** Add this line to `~/benchtop/backend/data/settings.env` (create the file if it isn't there), then close Benchtop and run `./benchtop lab` again:
    ```
    BENCHTOP_PUBLIC_URL=https://lab-mac.tail1234.ts.net
    ```
    From then on those links open only on devices with Tailscale on, in the lab too. If you use single sign-on, change its redirect address to this one as well ([SERVER.md](SERVER.md#single-sign-on)).
7. **Invite labmates** in the admin console under **Users → Invite users**. Remove people there too, and lost devices under **Machines**.
8. **Test it** from your phone with Wi-Fi off, then send labmates the address and this sheet.

After a restart of the lab Mac, run `./benchtop lab` again; Tailscale and its HTTPS address come back by themselves.
