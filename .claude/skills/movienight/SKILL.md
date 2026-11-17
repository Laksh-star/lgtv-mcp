---
name: movienight
description: Movie night on the LG TV. Ask who's watching, the mood and the time available, use Screenward to shortlist titles streaming in India on the apps installed on the TV, offer 3 picks with one-line reasons, then play the chosen one with lgtv play_title. Use for /movienight, "movie night", "what should we watch tonight" (with the TV), or "put on a <kind of> movie/series".
---

# /movienight

Needs two MCP servers: **screenward** (TMDB discovery, `mcp__screenward__*`) and **lgtv** (`mcp__lgtv__*`).
If either is missing, say which and stop.

## 1. Fill in the brief (ask only for what's missing)

From the user's words, take:
- **Who's watching**: just me / couple / family with kids / friends (sets `familySafe`, group size).
- **Mood**: map to Screenward moods `crowd | thriller | thoughtful | funny | family | mindbend`.
- **Time available**: max runtime in minutes (for a series: minutes per episode, or "an episode").
- Extras if given: language (Malayalam = `ml`, Hindi `hi`, Tamil `ta`, Telugu `te`, English `en`), a person
  (actor/director), movie vs series, titles to avoid.

If anything of the first three is missing, ask in **one** AskUserQuestion call (up to 3 questions,
no more). Don't ask about things the user already said ("a Clint Eastwood thriller under 2 hours" = mood
thriller, max 120 min, person Clint Eastwood; only "who's watching" is open, and for a solo-sounding
request you may assume "just me").

## 2. What can the TV play?

Call `mcp__lgtv__get_status`; if the TV is off, `mcp__lgtv__power_on`. Then `mcp__lgtv__list_apps` and
keep the streaming apps that are installed. Map them to Screenward service names:
Netflix -> "Netflix", Prime Video -> "Amazon Prime Video", JioHotstar -> "JioHotstar",
ZEE5 -> "Zee5", YouTube -> "YouTube" (rent/free only). Only recommend titles that **stream**
(subscription) on one of these in **IN**; skip rent/buy-only titles unless nothing else fits.

## 3. Shortlist with Screenward (country "IN")

Pass `excludeMovieIds` = the TMDB ids in `~/.config/lgtv/watch_history.json` (written by play_title;
read it with `cat`, it may not exist yet) so we don't re-suggest what was played recently.

- **Mood / runtime** -> `get_weekend_watchlist` (`mood`, `runtime`, `services`, `familySafe: "true"` for
  kids). For 3+ people with mixed tastes, `plan_watch_party` instead.
- **A language** (Malayalam, Tamil...) -> `advanced_search` with `language` + a fitting `genre` (Family,
  Comedy, Thriller...) and `minRating` ~7, sorted by `vote_count.desc`. (Seen 17 Nov 2026:
  `get_weekend_watchlist` ignored `language: "ml"` and returned English new releases.)
- **Availability**: with TMDB ids in hand, prefer `get_watch_providers` (movieId, IN) over
  `find_where_to_watch`, which matches by title and can pick the wrong film ("#Home" became *Home* 2015).
- **A person** ("a Clint Eastwood thriller") -> `search_person` + `get_person_details` for credits
  (TMDB's list can be thin and full of documentaries; add the person's well-known films in that genre
  yourself), keep films matching the genre/mood and runtime (check with `get_movie_details`), then
  check availability in IN. `build_person_watch_path` is a useful first scan, but it ignores genre and
  runtime. If nothing streams, say so and offer rent options (Prime Video store) as the fallback.
- **A series** -> `search_tv_shows` / `get_trending_tv` (or `build_tv_binge_plan` for "something to binge").
  Availability for TV: if no provider tool covers it, say availability is unverified.

## 4. Offer 3

Show exactly three options, best first, one line each:

`1. Title (year) · 1h 52m · Netflix: why it fits in one short clause`

Then ask which one (AskUserQuestion with the three titles; "Other" lets them type something else).

## 5. Play it

Call `mcp__lgtv__play_title` with `tmdb_id`, `title`, `media_type` ("movie"/"tv"), `app` = the service it
streams on (`netflix`, `prime`, `hotstar`, `zee5`, `youtube`), and `season`/`episode` for a series.

Handle the reply:
- `deep_linked`: check `playback.result`. "playing" means it's on. Otherwise, Read the image at
  `playback.screen_after` and finish the job with `press_button` (e.g. OK on a profile, or Play), or tell
  the user what's on screen. Netflix plays by itself; Prime Video ads may play first.
- `needs_lookup`: web-search the id using `id_hint`: Netflix `"<title>" netflix.com/title` (the number);
  Prime Video `"<title>" primevideo.com detail amzn1.dv.gti` (must be the `amzn1.dv.gti.<uuid>` form).
  If you find it with confidence, call play_title again with `app` + `app_content_id` (it is cached). If
  not, call again with `open_without_id: true`.
- A series episode usually comes back with a `note` that the show was opened (Netflix episode ids aren't
  public); tell the user it starts or resumes the next-up episode.
- `opened_app`: the app (JioHotstar, ZEE5: no working deep links) was opened with a toast; tell the user
  to pick their profile and search for the title. Prefer the Netflix/Prime option when a title is on both.
- `not_installed`: pick the next option's app, or offer the other options.

Keep the whole exchange short. No long preamble.

## 6. Watched list (optional)

play_title logs every launch to `~/.config/lgtv/watch_history.json`; that's what step 3 excludes.
Screenward's own saved watchlists live in its browser app (local storage), and the MCP server keeps no
personal state, so there is nothing to write there from Claude Code. If the user wants it in the
browser watchlist too, tell them to mark it watched there.
