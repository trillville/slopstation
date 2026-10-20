"""Process-creation flags the media heals share."""

import subprocess

# Out of the lane's kill-on-close job (supervise._die_together), so a service
# a heal relaunches is not ended by the next lane restart or deploy.
OUTSIDE_LANE = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
