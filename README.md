# tidyCH

tidyCH is a standalone Dispatcharr plugin for profile-scoped channel
logo assignment and safe channel-name normalization. The two features have
separate settings and actions. It does not require Lineuparr.

## Logo mapping

Set **GitHub Repository** to a public `owner/repository`. Its default branch is
detected automatically; `owner/repository@branch` is available for a non-default
branch.

Map profiles to repository folders:

```text
US Profile | us | logos/us
UK Profile | uk | logos/uk
```

Existing logos are preserved by default. Channels shared by mapped profiles
that select different logo sources are reported and skipped.

An exclamation mark is preserved as a meaningful part of a channel name. For
example, `Movies` matches `movies-us.png`, while `Movies!` matches
`movies!-us.png`.

## Channel name normalization

Add profile-specific trailing-marker rules:

```text
UK Profile | HD
Another Profile | HD, FHD
```

Only separate markers at the end of a name are removed. For example,
`Sky Atlantic HD` becomes `Sky Atlantic`; `HD` appearing inside a name is not
changed. Name normalization changes only `Channel.name`, preserving the same
channel ID and its streams, EPG assignment, logo, number, group, and profile
memberships.

Safety behavior:

- Preview and apply are separate actions.
- A same-group name collision is skipped and reported.
- Channels shared with an unmapped profile are skipped because Dispatcharr has
  one global name per channel.
- Channels shared by profiles with different rules are skipped.
- Every applied run is recorded in `/data/channel_curator_name_history.json`.
- Rollback restores only channels that still have the normalized name and whose
  original name does not now collide.

## Optional Lineuparr compatibility

tidyCH can generate a normalized copy of an existing Lineuparr JSON
file. The clean name becomes the lineup's canonical channel name and the
original name is appended to that entry's `aliases` list for Lineuparr's stream
and EPG matching only.

```json
{
  "name": "Sky Atlantic",
  "aliases": ["Sky Atlantic HD"]
}
```

For each Dispatcharr profile, choose the same lineup that profile uses in
Lineuparr. The selectors display the provider name and exact filename from the
installed Lineuparr lineup library. `Full` and `Simple` are variants supplied by
Lineuparr; they are not tidyCH processing stages.

tidyCH derives the generated filename automatically. For example,
`UK_SkyTV_ENG_full_lineup.json` produces
`UK_SkyTV_ENG_full_tidych_lineup.json`. The source is never modified. An existing
generated output is backed up before replacement. Generation stops on
normalization collisions. Select the generated lineup in Lineuparr only after
reviewing it. Lineuparr remains optional and no Lineuparr code is modified.

Browse the upstream lineup library:
https://github.com/PiratesIRC/Dispatcharr-Lineuparr-Plugin/tree/main/Lineuparr

## Reports

Logo previews, logo applications, name previews, name applications, and
Lineuparr generation reports are written as CSV files under `/data/exports`.
