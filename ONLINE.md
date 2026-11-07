# Playing online with friends

The launcher's **Online** tab lets you play The Duskbloods Network Test with friends. Everyone who
types the **same room password** is matched together. One player **hosts** the room and the others
**join** it.

> **Experimental.** Online matches are still being tested. If something does not work, see
> [Reporting a problem](#reporting-a-problem). Those logs are what get it fixed.

## Contents

1. [How it works](#how-it-works)
2. [Connecting players (read this first)](#connecting-players-read-this-first)
3. [Recommended: ZeroTier](#recommended-zerotier-step-by-step)
4. [Other options: Tailscale, Radmin VPN, same home network, port forwarding](#other-options)
5. [Hosting a room](#hosting-a-room)
6. [Joining a room](#joining-a-room)
7. [Troubleshooting](#troubleshooting)
8. [Privacy](#privacy)
9. [Reporting a problem](#reporting-a-problem)

## How it works

* The **host** runs the small stand-in servers that the launcher always runs, but makes them
  reachable from other PCs. The host's PC is the "meeting point" for the room.
* **Joiners** don't run servers. Their game talks to the host's servers.
* When players start matchmaking in the game, the host's server groups everyone who used the same
  room password into one match. The game then **connects the emulators directly to each other**
  (peer-to-peer).

Everyone needs:

* the latest `Play Duskbloods.exe` from the [Releases](https://github.com/sikoGD/playDB-CNT/releases)
  page (everyone on the same version),
* their own copy of the game, emulator and keys (set up in the **Setup** tab, as for offline play),
* a way to reach the host's PC. See the next section.

## Connecting players (read this first)

**The in-game match connects the emulators directly, and the server has no relay for that.**
Players need one of these:

| Option | Good for | Difficulty |
|---|---|---|
| **Same home network** (same router / Wi-Fi) | friends in the same house | nothing to set up |
| **[ZeroTier](https://www.zerotier.com/download/)** (recommended) | friends anywhere, Windows and Linux | easy, about 5 minutes |
| **[Tailscale](https://tailscale.com/download)** | friends anywhere, Windows and Linux | easy |
| **[Radmin VPN](https://www.radmin-vpn.com/)** | friends anywhere, Windows only, no account | very easy |
| **Router port forwarding** | no VPN software at all | harder; needs router access |

A virtual network (ZeroTier, Tailscale, Radmin VPN) makes your PCs act as if they were on the same
home network. They're free for small groups and need no router changes.

**Everyone in the room must use the same option.** If the host uses ZeroTier, every joiner joins the
same ZeroTier network.

## Recommended: ZeroTier (step by step)

### The host creates the network (once)

1. Install ZeroTier from <https://www.zerotier.com/download/> and start it. On Windows a small
   ZeroTier icon appears next to the clock.
2. Create a free account at <https://my.zerotier.com> and click **Create A Network**.
3. Click the new network. Copy its **Network ID**, a 16-character code like `8056c2e21c000001`.
4. Leave **Access Control** on *Private*. You'll approve each friend yourself (step 7).
5. On your own PC: right-click the ZeroTier icon, choose **Join New Network...**, paste the Network
   ID and press **Join**.
6. Send the **Network ID** to your friends.
7. Back on my.zerotier.com, scroll to **Members**. Each PC that joined appears there. Tick the
   **Auth** checkbox for yourself and for each friend. Each member gets a **Managed IP**, for
   example `10.147.17.23`.

### Each friend joins the network

1. Install ZeroTier and start it (no account needed).
2. Right-click the ZeroTier icon, choose **Join New Network...**, paste the host's Network ID and
   press **Join**.
3. Wait until the host ticks **Auth** for you. The network then shows as *OK*.
4. When Windows asks whether to allow the new network to be found, answer **Yes**.

### Check it works

The host opens the launcher, goes to **Online → Host a room** and looks at **Your address**.
One of the addresses listed is the ZeroTier one (the same Managed IP shown on my.zerotier.com, often
`10.x.x.x` or `172.x.x.x`). Give your friends **that** address.

> Linux: `curl -s https://install.zerotier.com | sudo bash`, then
> `sudo zerotier-cli join <Network ID>`.

## Other options

### Tailscale

1. Everyone installs Tailscale from <https://tailscale.com/download> and signs in.
2. The host adds friends to their network. In the [admin console](https://login.tailscale.com/admin/users)
   use **Invite users**, or **Share** the host's PC with each friend from the Machines page.
3. The host's Tailscale address starts with `100.`. It appears in the launcher under
   **Your address**. Give that one to your friends.

### Radmin VPN (Windows only, no account)

1. Everyone installs Radmin VPN from <https://www.radmin-vpn.com/>.
2. The host clicks **Network → Create network** and chooses a network name and a network password.
   This is not the room password.
3. Friends click **Network → Join network** and type the same name and network password.
4. The host's Radmin address starts with `26.`. It appears in the launcher under **Your address**.

### Same home network

Nothing to install. The host gives friends the address that starts with `192.168.` (or `10.`) shown
under **Your address**.

### Router port forwarding (advanced)

Without a VPN, the **host** forwards these ports on their router to their PC, and gives friends
their **public** IP address (search "what is my IP").

| Protocol | Ports |
|---|---|
| TCP | 18443, 18445 |
| TCP and UDP | 3478-3479 |
| UDP | 10025, 10125, 33334, 34343 |

Joiners may also need UDP 10025, 10125, 33334 and 34343 open, because the match connects the
emulators directly. Many home connections (CGNAT, mobile hotspots) can't receive incoming
connections at all. A VPN from the list above avoids all of this.

## Hosting a room

1. Set up the **Setup** tab as usual: game folder and emulator.
2. Connect everyone (see [Connecting players](#connecting-players-read-this-first)).
3. Open the **Online** tab and click **Host a room**.
4. Type a **room password**. Anything works, for example `blood-moon-42`.
5. Send your friends:
   * the **room password**, and
   * the right **address** from **Your address** (use **Copy**). That's the VPN address if you use a VPN.
6. Press **Start game**. When Windows Firewall asks, click **Allow access** for **both private and
   public networks**. Do this for the launcher and for the emulator.
7. In the game, start online matchmaking. Your friends do the same.

Keep the launcher open while you play. Closing the game stops the room for everyone.

## Joining a room

1. Set up the **Setup** tab as usual: game folder and emulator.
2. Connect to the host's network (ZeroTier, Tailscale, Radmin VPN or the same home network).
3. Open the **Online** tab and click **Join a room**.
4. Type the **same room password** as the host. Capitals and spaces count.
5. Type the **Host's address** they sent you, for example `10.147.17.23`.
6. Wait until the host has pressed **Start game**, then press **Start game** yourself.
7. Allow the emulator in Windows Firewall if asked, then start online matchmaking in the game.

## Troubleshooting

**"Could not reach the room at ..."**
* Did the host press **Start game** first? The room only exists while the host's game runs.
* Is the address right? It must be the host's address on the network you share. With ZeroTier,
  check that both of you show *OK* and are ticked under **Auth**.
* Test the connection: open a Command Prompt and run `ping <host address>`. If ping fails, the
  VPN isn't connected yet. If ping works but joining fails, it's the host's firewall. The host should
  allow the launcher in Windows Firewall (Settings → Privacy & security → Windows Security →
  Firewall & network protection → Allow an app through firewall), tick **Private** and **Public**,
  and restart the room.

**Matchmaking never finds anyone.**
* Everyone must use the **exact same room password**. A different password is a different room.
* Everyone must start matchmaking within roughly the same minute.
* Everyone must use the same launcher version.

**Matched, but the game says the connection failed or someone drops out.**
* The direct emulator-to-emulator connection is blocked. Allow the **emulator** through Windows
  Firewall on every PC (Private and Public), or switch to a VPN from the list above.
* Some VPNs on Windows count as a *Public* network. Ticking **Public** in the firewall rules fixes
  that.

**My VPN address is missing from "Your address".**
* Connect the VPN first, then click **Host a room** again (the list refreshes), or restart the
  launcher.

**"Port 18443 is still in use"**
* An earlier session is still closing. Wait a few seconds, or close the other emulator or launcher
  and try again.

## Privacy

* **Just me (offline)**: nothing leaves your PC (127.0.0.1 only).
* **Online rooms**: the password is never sent. The launcher only sends a hash (a fingerprint) of
  it to the host, so the host can put you in the right room. The host's PC sees your address and
  your in-game requests. Your keys, game files and saves are never sent.
* Only play with people you trust: hosting opens the ports listed above on your PC while the game
  runs.

## Reporting a problem

Open an [issue](https://github.com/sikoGD/playDB-CNT/issues) and attach, **from the host and from
one joiner**, the newest folder from **Open logs** (in the launcher). The host's `frontdoor.log`
shows exactly what the game asked during matchmaking. Before sharing logs, check them for anything
private.
