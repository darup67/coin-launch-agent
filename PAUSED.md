# PAUSED 2026-10-06

The user paused the coin detector and plans to replace it with the Jev desk (~/jev-desk).

Paused (unloaded; each plist renamed to `.plist.disabled` in ~/Library/LaunchAgents):
- `com.dhruv.coinlaunch`: the watcher, which also ran Jev shadow judgments on hits
- `com.dhruv.coinlaunch.listed`: the Coinbase/Robinhood listed-coin board and coin-detector emails

Still running: `com.dhruv.coinlaunch.sentiment`. The crypto scanner's alert emails and the
08:55/16:30 briefs read its readings. Its coin list comes from `data/listed_board.json`, which is
now frozen at the last board; the Coinbase/Robinhood listed set rarely changes.

The watchdog sees this file and skips the coin watcher and listed-board freshness checks.

## Resume
```bash
cd ~/Library/LaunchAgents
for l in com.dhruv.coinlaunch com.dhruv.coinlaunch.listed; do mv $l.plist.disabled $l.plist && launchctl load $l.plist; done
rm ~/coin-launch-agent/PAUSED.md
```
