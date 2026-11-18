"""A login the server ends after too many keys ("Too many authentication failures": ssh offers the agent's keys before
the configured one, as at CSCS with many keys in the agent): slurp logs in again once, with only the configured keys
(IdentitiesOnly=yes), and says how to keep it so; not a second time, not for other failures"""
import types

from slurp.ui.app import Slurp


def test_too_many_keys(check):
  events, notes, logins = [], [], []
  c = types.SimpleNamespace(ssh_options=["-p", "22"], event=events.append)
  cluster = types.SimpleNamespace(name="daint", destination="daint-alps", connection=c, show_login=False, broken_at=1.0)
  app = types.SimpleNamespace(clusters=[cluster], notify=lambda message, **kw: notes.append(message),
                              login=lambda cl, show=True: logins.append(cl))
  said = ["Received disconnect from UNKNOWN port 65535:2: Too many authentication failures", "Disconnected from UNKNOWN port 65535"]
  check("other failures: nothing", not Slurp.too_many_keys(app, c, ["Permission denied (publickey)."]) and not logins)
  check("too many keys: again", Slurp.too_many_keys(app, c, said) and logins == [cluster])
  check("…with only the configured keys", c.ssh_options == ["-p", "22", "-o", "IdentitiesOnly=yes"], c.ssh_options)
  check("…said, with how to keep it so", notes and 'IdentitiesOnly yes' in notes[0] and "Host daint-alps" in notes[0], notes)
  check("…in its events", any("IdentitiesOnly=yes" in e for e in events), events)
  check("not a second time", not Slurp.too_many_keys(app, c, said) and len(logins) == 1)
