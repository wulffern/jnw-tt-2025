![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg)

# JNW-TEMP — two temperature sensors

Two temperature sensors designed by students on NTNU's TFE4188 *Advanced
Integrated Circuits* course, taped out as Tiny Tapeout project **258**
(`tt_um_jnw_wulffern`) on the ttsky25a shuttle in sky130.

- **[Measurement slides](https://analogicus.com/jnw-tt-2025/presentation.html)** —
  how the two sensors work, the measurement setup, and what 15 minutes of both
  running side by side actually showed
- [Project datasheet](docs/info.md) — the students' own description of each circuit
- [`meas/`](meas/) — `jnwtemp`, the live readout used to take those measurements
- [3D GDS viewer](https://analogicus.com/jnw-tt-2025/)

## The sensors

Both are a PTAT current charging a capacitor into a comparator, so the time they
produce is inversely proportional to absolute temperature. They differ in how
that time is read out:

| | GR07 | GR06 |
| :--- | :--- | :--- |
| Output | `uo_out[0]` — free-running PWM | `uo_out[2]` — one pulse per reset |
| Observable | period, 1099 ns at 23 °C | pulse width, 7078 ns at 23 °C |
| Stimulus | none, just listen | pulse `ui_in[0]` (ResetTemp06) |
| Precision per 0.5 s | 3.3 mK | 12.8 mK |
| Best stability | 57 mK at τ = 8 s | **31 mK at τ = 76 s** |

GR07 is ~4× more precise per reading — it gives ~200× more events — but its
period is re-timed by the project clock and it drifts, so averaging stops helping
after a few seconds. GR06 keeps improving out to a minute. Neither tracks the
other: their fluctuations are statistically independent, so on a die this small
what the spread measures is the sensors, not the room. The slides go into why.

## What is Tiny Tapeout?

Tiny Tapeout is an educational project that aims to make it easier and cheaper than ever to get your digital designs manufactured on a real chip.

To learn more and get started, visit https://tinytapeout.com.

## Analog projects

For specifications and instructions, see the [analog specs page](https://tinytapeout.com/specs/analog/).

## Enable GitHub actions to build the results page

- [Enabling GitHub Pages](https://tinytapeout.com/faq/#my-github-action-is-failing-on-the-pages-part)

## Resources

- [FAQ](https://tinytapeout.com/faq/)
- [Digital design lessons](https://tinytapeout.com/digital_design/)
- [Learn how semiconductors work](https://tinytapeout.com/siliwiz/)
- [Join the community](https://tinytapeout.com/discord)

## What next?

- [Submit your design to the next shuttle](https://app.tinytapeout.com/).
- Share your project on your social network of choice:
  - LinkedIn [#tinytapeout](https://www.linkedin.com/search/results/content/?keywords=%23tinytapeout) [@TinyTapeout](https://www.linkedin.com/company/100708654/)
  - Mastodon [#tinytapeout](https://chaos.social/tags/tinytapeout) [@matthewvenn](https://chaos.social/@matthewvenn)
  - X (formerly Twitter) [#tinytapeout](https://twitter.com/hashtag/tinytapeout) [@matthewvenn](https://twitter.com/matthewvenn)
