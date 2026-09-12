# Reliability Log

**Phase 1 objective: reliability, not optimisation.** There's no baseline yet —
so the job right now isn't to make the process faster or cheaper, it's to make
sure what we *record* about a part is actually true, and to catch the specific
ways a shop-floor record normally goes wrong before it causes a bad decision.

Every entry below is a real thing this system caught or fixed on real parts
(Bottom Plate / SE-004-052, Top Plate / RKSE-004-053), written so a machinist
or CNC programmer reading it would say "yeah, that happens" — not a
data-science abstraction. New entries get added here as they come up; this is
not a one-time list.

---

### The QC report can get swapped without anyone noticing — and it did
Bottom Plate's inspection file changed mid-project: material label flipped
from Aluminum to Copper, and the "78" and "108" dimensions went from passing
comfortably to reading 0.04–0.05 mm over. Nobody flagged it — it was just a
different spreadsheet that looked the same. **Because the pipeline re-reads
the actual file every time instead of trusting a remembered PASS from last
week, the part's status flipped from PASS to FAIL automatically the moment
the real inspection numbers changed.** Without that, the shop would still be
treating that part as passed.

### A tapped hole was about to be logged as a scary failure — it isn't one
Top Plate's SL8 ("Ø4.4, tapped hole") measured 3.57–3.60 mm against a 4.4 mm
nominal — an 0.8 mm miss, which looks like a wrecked hole. But the drawing
note says "tapped hole": 4.4 is very likely the thread's major diameter,
while what actually gets measured is the tap-drill/minor diameter — a
different number by design, not a defect. **Flagged before it became "this
operation is bad" instead of "this dimension is being compared to the wrong
reference."** That's the difference between chasing a real problem and
chasing a paperwork mismatch.

### Telemetry from a different job was about to be blamed on this part
Late in the day on Top Plate, `1_10end` and `2_10end_1` ran again — but that
was a **different part**, reusing the same program names hours after Top
Plate was actually finished. This is an everyday shop thing (programs get
reused, nobody renames them for a one-off job) — but if it goes unnoticed, a
part's "actual" feed/speed/cutting-time numbers quietly belong to the wrong
job. **Caught by cross-checking real timestamps against what the operator
confirmed, and excluded from Top Plate's numbers — while still counting
toward the day's overall machine time**, so nothing real gets thrown away,
it just gets attributed correctly.

### An hour-plus of real machine time was invisible until we checked
Three separate stretches of real, logged machine activity — 26 minutes,
46 minutes, and 8 minutes with real cutting in it — didn't match any program
name in the CAM folder and would have silently vanished from every report.
That's the kind of gap where, six months from now, someone asks "what was
the machine doing between 4 and 5am that day" and the honest answer used to
be "no idea." **Now the report calls these out by name and by minute instead
of quietly dropping them**, so a foreman/programmer can go check what
actually happened instead of the record just being wrong by omission.

### "Stopped" was quietly including time the operator was working by hand
Whenever an operator jogged the machine manually — positioning, touching
off, hand-feeding a cut — the machine's own status flag reads the same as a
plain pause, so it was being counted as dead time. That means every
utilisation number was making an operator's real, hands-on work look like the
machine sitting idle. **Decoded the machine's own auto/manual signal and
split it into its own bucket**, so "Stopped" now means actually stopped, and
manual work shows up as manual work — not as the machine looking worse than
it was.

### A part that ran twice was reporting the wrong run's numbers
`1_10end` ran on Top Plate twice, hours apart. The report's own bookkeeping
was silently keeping the *second* run's position label next to the *first*
run's actual feed/speed data — an internal mismatch that would have shown a
programmer a made-up combination that never actually happened on the machine.
**Fixed so a repeated program always shows the numbers from the run it says
it's showing**, and flags that it ran more than once so nobody mistakes a
re-run for a single clean pass.

### 61 alarms on one hole — and the part still passed
A deep, small drilled hole threw 61 alarm events and still measured in spec.
Turned out the alarm flag fires on every single chip-clearing retract during
deep drilling — completely normal for that cycle, not a sign of trouble.
**Confirmed this pattern is noise on this machine before it could train
anyone to either ignore alarms altogether (dangerous) or panic over a normal
cycle (wasted time)** — the alarm count is now shown as "worth a glance," not
"something broke."

### The utilisation number was structurally wrong — a perfect day still read ~70%
The "how much of the window do we actually have data for" number was
computed in a way that assumed the machine reports a reading every single
second. It doesn't — never has, even when everything is working — so a
completely clean, gap-free day was reading 67% instead of the ~95%+ it
actually was. **A shop reading that number would think there was a data
problem on a day when there wasn't one.** Fixed to measure actual gaps
(PC/collector being off) instead of sampling rate, and the two are now shown
separately so neither hides the other.

### The operator's real feed-and-speed decisions were invisible until now
Until 9 Sep, there was no way to see what the operator actually dialled the
feed override to — only the end result (actual feed rate), which mixes in
lead-ins, rapids, and setup moves and can't be told apart from the machine
just not being able to accelerate fast enough on short moves. Now that the
real feed-override signal is live, we can see, operation by operation,
exactly what the operator chose — e.g. pushing roughing to 140–200% but
backing finishing off to 50–90%. **That's the difference between guessing at
operator behaviour from a blended average and reading their actual decision
directly** — the foundation for ever being able to say "this is what a good
operator does differently from a risky one," which is a reliability question,
not a speed one.

---

*Keep adding to this. The rule for a new entry: it should make a machinist,
programmer, or shop supervisor nod and say "yeah, that's a real thing that
happens here" — not require them to trust a statistic.*
