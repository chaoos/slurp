"""Transfers: data from one tab (a cluster, or this machine) to another, with rsync.

- model.py     a transfer (what, from where, to where, how, when), its runs, the store of all of them
- rsync.py     rsync's command lines, its progress (--info=progress2) and its statistics
- host.py      a tab as a host: where the commands and channels of a transfer run (its SSH session, or here)
- relay.py     the relay: rsync on the source, its connection to an rsync server on the destination through slurp
- runner.py    a run on a cluster (detached on the login node, or as a Slurm transfer job): its folder, its script
- plan.py      the check before a transfer: paths, what remains, room, the routes and their speed, the estimate
- schedule.py  when a transfer runs next
- manager.py   the transfers of the app: runs them, follows them, pauses and continues them, their schedules

A transfer is idempotent: afterwards the destination contains the sources (rsync skips what is there already, keeps
half-copied files in .slurp-partial and goes on from them; it never deletes on the destination); a second run while
one runs is not started (the lock in the run's folder, the job name of a transfer job)."""
