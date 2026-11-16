# Quota and scratch

**Quota** (in the menu, or `Q`, a subtab that loads in the background) lists your quotas, and those of your groups where they
use space or have a limit: `lfs quota` on each Lustre filesystem and on
`$SCRATCH`, `$HOME`, `$PROJECT`, `$STORE`, `$WORK`, `quota` on the others,
with space and files (inodes), used, limit and soft limit, and a bar coloured
by how full it is. The output of a site's own `quota` command is shown too.

Scratch filesystems are usually cleaned up automatically: files not used for
some time are deleted without asking. **Old files in scratch** (in the menu, or `X`) searches your files
in `$SCRATCH` in the background (one `find` on the cluster; it can be
aborted) and lists the 2000 with the oldest access (or modification) time;
those older than the given number of days (30) are red, those in its last fifth
yellow. An exclude regex leaves matching paths out of the list and of every
touch. **Touch selected**,
**Touch all listed** and **Touch all my files there** set their access time
to now (after a confirmation). Mind that many sites do not allow keeping files
in scratch this way; move what you need to keep elsewhere.
