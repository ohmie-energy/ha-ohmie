# Ohmie for Home Assistant

Ohmie plans when your battery, EV, hot water and pump use power: at the cheapest times on New
Zealand's grid, inside the limits you set. By default Ohmie only **suggests**: the plan shows in Home
Assistant, and you decide. When you allow it, per device, Ohmie **commands** the device.

**Ohmie never touches your equipment.** It changes only its own entities: an Ohmie switch, a
Command, a charge window. Your own automations pass them to your devices, under your own
conditions. Give every one of them a condition such as an input boolean "Ohmie enabled", and you
can cut Ohmie off from your equipment at any time, from anywhere. Ohmie has no code for any inverter
or car brand, and needs no vendor cloud or VPP programme.

Ohmie membership is invite-only for now.

## Install

1. In HACS, add this repository as a custom repository (category: Integration).
2. Install **Ohmie**, then restart Home Assistant. HACS installs the latest
   [release](https://github.com/ohmie-energy/ha-ohmie/releases) and tells you when a new one is out.
3. Go to **Settings → Devices & services → Add integration → Ohmie**.

## Setup

The wizard asks only what Ohmie needs:

1. **Sign in or sign up** at `auth.ohmie.energy`. Use a passkey if you can.
2. **Your home.** Ohmie uses your Home Assistant location once to find the nearest grid node, then
   forgets the location. Your ICP is optional: with it, plans use your total price (lines charges
   and losses). Ohmie stores it encrypted and never shows it again.
3. **Devices.** Devices already in Ohmie come first. Then Ohmie lists the devices it found here
   (it recognises the sensors of SolaX, Tesla, Shelly, Tuya and TP-Link, and solar and grid from
   the Energy dashboard). Ohmie fills in typical specs; you confirm the essentials.
4. **Style:** save the most, balanced, or keep a buffer for a power cut.
5. **Your power plan**, read off your bill: spot (plus your retailer's margin, per kWh or as a
   percentage), time of use (up to ten windows: peak, shoulder, off-peak, as your retailer defines
   them), or one price; the same for exports; and a controlled (ripple) register for devices such
   as hot water, at its own rate. Ohmie then plans on what you really pay. Changed retailer and
   forgot to tell Ohmie? Configure → Change my power plan takes the date it really started.
6. **Suggest or drive**, per device. To let Ohmie drive, you accept Ohmie's terms.

A second site (a bach) is a second Ohmie entry, set up from the Home Assistant at that site.

## What you get

Each device is a Home Assistant device, **Ohmie · _name_**:

| Entity | What it shows |
|---|---|
| Action | charge, hold, discharge, or run, idle (a pump) |
| Setpoint | kW for this half hour; the whole plan is in its `plan` attribute |
| Reason | why, in plain words |
| Mode | Suggests or Drives |
| Run now | on when the plan says charge or run: for your own automations |
| Let Ohmie drive | your consent for this device (the home's owner only) |
| **Ohmie · _name_** (a switch) | hot water, a pump, an EV: Ohmie's own switch, on to run or charge. Pass it to the real switch with the *pass an Ohmie switch* blueprint |
| Target charge, Ready by, Run hours, Run every | your targets; a change goes to Ohmie |

Also per device: **Command**, **Next charge**, and for a battery **Next export** and **Reserve**
(see below).

For the site: **Pause Ohmie** (stops Ohmie at once, also offline), **Price now** (NZD/kWh, ex GST:
what your plan charges now; without a plan, your total price from your ICP, else the nodal price),
**Cheap now** and **Next cheap window** (your plan's cheapest window on time of use, the market's
cheap periods otherwise), **Connected**, and a **Plan** calendar.

## When Ohmie commands

- A change you make by hand to an Ohmie switch wins until the next half hour.
- With no current plan (at most an hour), each device goes back to its own normal mode.
- **Pause Ohmie** hands every device back at once: every Command goes to `normal`, every Ohmie
  switch to its normal state.
- Ohmie keeps devices inside the limits you gave it.

| Device | How Ohmie commands it |
|---|---|
| A relay or smart plug (hot water, a pump) | the **Ohmie switch**, wired by the *pass an Ohmie switch* blueprint |
| A battery or inverter that takes a mode and a power | **Command** (`charge`, `discharge`, `hold`, or `normal` for your own mode) with its power, wired by the *mode and power* blueprint |
| A battery or inverter that takes a time window and a target charge | **Next charge** / **Next export** with the target charge level, wired by the *charge window* blueprint |
| An EV or charger with a charge switch and a current setting | **Command** with its power, wired by the *EV charge current* blueprint |

When your automation acts, it can call **`ohmie.report`** to tell Ohmie about a change you made by
hand (`overridden`) or a refusal (`failed`). An override hands the device back until the next half
hour. Ohmie also checks from the device's readings whether it followed the plan.

## Blueprints

In `blueprints/automation/ohmie/`:

One per control shape, none per brand. Each one takes an optional "only while this is on" entity.

- **Command a battery by mode and power.**
- **Set a charge (or export) window and target charge.**
- **Charge an EV at Ohmie's current.**
- **Pass an Ohmie switch to your device.** Also from "Run now", without letting Ohmie command.
- **Tell me what Ohmie plans.** A notification when a plan changes.

## Privacy

Home Assistant sends Ohmie the readings of the entities you connected (charge level, power, energy,
temperature), every 5 minutes. Ohmie keeps no location, name or address: only the grid node and your
encrypted ICP. Diagnostics leave out your sign-in and your ICP.

## Development

```bash
uv sync --group dev
uv run pytest
```

- The NATS tests run a real `nats-server` (set `NATS_SERVER_BIN`). They are skipped without one.
- The declaration tests check the wizard's output against the Ohmie CRDs: put the `gitops`
  checkout next to this one, or set `GITOPS_CRDS`. They are skipped without it.
- Releases: see [RELEASING.md](RELEASING.md). Every change adds a line to [CHANGELOG.md](CHANGELOG.md).
- The contract is in `foundation-documents`: `api-specs/home-assistant-contract.md` and
  `api-specs/edge-protocol.md`.

## Licence

Apache License 2.0. See [LICENSE](LICENSE).
