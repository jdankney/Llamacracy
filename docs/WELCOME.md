# Welcome to Llamacracy 🦙

Hey — you're getting this because I built a thing and I want you to use it.

**Llamacracy** is my own little AI chat service. It runs entirely on my desktop
at home — one graphics card doing all the thinking — and it's yours to use.
Think ChatGPT, except it's a hand-built home-grown version where the electricity
bill has a face, and that face is mine.

## What you get

- A clean chat UI with **6 local models** to pick from — a fast little one for
  quick stuff, a couple of mid-size ones, and a 35-billion-parameter monster
  for long documents and hard problems.
- Proper markdown rendering, code with syntax highlighting, rendered math for
  the physics/maths crowd, answers that stream in as they're written — and a
  **copy button** on every message so you can pull the raw markdown/LaTeX
  straight into your own notes.
- Drop an image into the composer and the two biggest models can see it
  (describe it, read text in it, answer questions about it).
- A **search toggle** in the composer — flip it on and that message gets a
  real web search first, with sources cited underneath, before the model
  answers. Off by default.
- Your own conversation history and a personal usage page. Long chats can be
  **compacted** on demand (a button in the context panel) to free up room
  without losing the thread — it folds the older part into a summary, it
  doesn't delete anything.
- Installable as an actual app icon on your phone (see below) — not just a
  bookmark.
- No signup circus, nothing sold to anyone, no "as an AI language model, I
  can't help with that." Some of these models are... not very censored.

## Getting in (about 2 minutes)

1. **Install NetBird** and join my network — I'll send you an invite link
   separately. NetBird is a tiny always-on mesh VPN; it's just how your laptop
   and my desktop find each other. Download: <https://netbird.io/download>
2. When NetBird says **Connected**, open:
   **<http://myhost.netbird.selfhosted:4180>**
   - If the address doesn't load, your NetBird DNS is switched off. Turn on
     DNS in the NetBird app and try again (or ping me and I'll sort it).
3. Log in with the email + password I send you. Done.

If you're not connected to NetBird, the site simply doesn't exist for you —
it's never on the public internet. That's deliberate.

### On your phone

Same deal — NetBird app connected, then open the site in **Safari** (iPhone)
or **Chrome** (Android). Then:

- **iPhone:** tap Share → **Add to Home Screen**.
- **Android:** tap the menu (⋮) → **Install app** (or Chrome may offer a
  banner on its own).

You'll get a real icon that opens full-screen, no browser address bar — no
App Store, nothing to install from anywhere but the site itself. You still
need NetBird connected for it to load, same as on a computer.

## The one thing to understand: there's only one machine

Everything runs on a single GPU, **one model at a time, strictly
first-come-first-served**. So:

- If someone else is mid-request, you'll see a **live queue** and your position
  in it. Usually it's seconds.
- Switching to a different model means unloading one and loading the next
  (~10–25 seconds). The app tells you when that's happening and roughly how long.
- There's a **cancel** button. If you fired off something huge and changed your
  mind, hit it — it hands the box back to everyone else immediately.
- After a minute of quiet the model unloads so I get my memory back, so the
  next person pays a small "warm-up". No big deal.

Basically: it's a shared sauna, not a row of private showers. Be a good
roommate and it's great.

## Credits & limits (they're generous, and they don't bite)

Usage is measured in **credits**, where `1 credit = 1 second of the GPU working
just for you`. Waiting in the queue is free. The model reading your prompt is
free. You're only charged for the seconds it spends actually generating your
answer.

- **Session:** ~3,600 credits per rolling 5-hour window — that's a full hour of
  pure generation, which is a *lot* of back-and-forth.
- **Weekly:** ~12,000 credits per rolling 7 days.

You'll get a gentle heads-up at 75% and 90%. And the nice part: **overshoot is
allowed.** If you're under your limit when a request starts, it runs all the way
to the end even if it goes over — you're never cut off mid-answer. You just
can't *start* a new one until the rolling window drops you back under, which
happens on its own. Nothing to top up, nothing to pay.

Doing something real — a project, a big writing job — and need more headroom?
Just ask. I can raise your limits individually.

## The models, at a glance

| Model | Reach for it when… |
|---|---|
| **FamilyA 4B** | you want a fast answer — summaries, reformatting, quick questions |
| **FamilyA 9B** | general daily use; runs fully on the GPU, nice and quick |
| **FamilyB 4B** *(alt)* | the stock models are being prissy |
| **FamilyB 26B QAT** | you want stronger quality and don't mind a short wait |
| **FamilyC Flash** | accuracy-first factual Q&A |
| **FamilyA 35B** | the big gun — long documents, tougher reasoning |

"Thinking" / chain-of-thought is off by default to save time and power. Just
ask your question normally.

## What it can't do (yet)

- Image input only works on the two biggest models, and only the current
  message's image is "seen" — it won't re-look at a photo from three messages
  ago on a follow-up.
- Each conversation stands on its own — no shared memory between chats (your
  history is saved, though, and you can compact a long one instead of losing
  it).
- No knobs for temperature and such — I've set sensible defaults per model.
- It's a home server. If the power blips or I reboot the machine, it'll drop
  for a minute. It comes back.

## And yes, before you ask

There's a dashboard that tells me what everyone's usage costs in actual
electricity — the utility's rates, not mine, so take it up with the state. I'm not
going to invoice you. Probably. It mostly exists so I can watch the number go
up and feel something.

---

Go try it. Ask it something. Send me the strangest thing you can get it to say.

— j4mes
