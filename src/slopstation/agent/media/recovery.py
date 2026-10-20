"""After a heal: ask the apps to search again for what they gave up on.

Neither app goes back by itself. Its RSS sync only sees new uploads, so a
release rejected while the stack was broken stays missing until someone
searches for it.
"""

from slopstation.agent.media.clients import MediaError, clean_text

# The command each app's own "Search All Missing" button sends.
MISSING_SEARCH: dict[str, dict[str, object]] = {
    "Sonarr": {"name": "MissingEpisodeSearch", "monitored": True},
    "Radarr": {
        "name": "MissingMoviesSearch",
        "filterKey": "monitored",
        "filterValue": "true",
    },
}


def search_missing(clients, log, after):
    """Queue a search for every monitored item without a file. `after` names
    the heal that made it worth trying again."""
    for client in clients:
        try:
            client.post("command", MISSING_SEARCH[client.name])
        except MediaError as e:
            log.error(
                "media_missing_search_failed",
                app=client.name,
                after=after,
                err=clean_text(e),
            )
            continue
        log("media_missing_searched", app=client.name, after=after)
