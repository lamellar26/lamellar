"""Pre-execution interception: every tool call passes through the rules once, with the result allow / deny / rewrite / ask.
Default restricted zones (ask a human): credential files, system settings, scheduling, service management. No answer means refusal.
Industry packs can add their own rules through guard_rules in the config."""
import os, re

# (zone, command regex, reason)
_CMD_ZONES = [
    ('credentials', r'(?<![\w/.-])(htpasswd|passwd|chpasswd|ssh-keygen|ssh-copy-id|gpg\s+--(gen|import)|openssl\s+passwd|useradd|usermod|adduser|visudo)(?![\w-])',
     'This command creates or changes login credentials'),
    ('credentials', r'(>>?|tee(\s+-a)?|cp\s|mv\s|sed\s+-i|rm\s)[^|;&]*(\.htpasswd|authorized_keys|id_(rsa|ed25519|ecdsa)|/etc/(shadow|passwd|sudoers)|\.netrc|\.pgpass)',
     'This command writes to or changes a credentials file'),
    ('scheduling', r'(?<![\w/.-])(crontab(?!\s+-l\b)|at\s+\S|atq|atrm|systemd-run)(?![\w-])|/etc/cron|/var/spool/cron',
     'This command changes scheduling (jobs that run automatically on a timer)'),
    ('services', r'(?<![\w/.-])(systemctl\s+(start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload|edit|kill|set-default|isolate)|service\s+\S+\s+(start|stop|restart|reload)|pm2\s+(start|stop|restart|reload|delete|kill|save|startup)|shutdown|reboot|poweroff|halt|init\s+[0-6])(?![\w-])',
     'This command starts, stops or restarts a system service'),
    ('system', r'(?<![\w/.-])(sudo|su\s+-|sysctl\s+-w|ufw|iptables|nft|mount|umount|chown\s+-R\s+\S+\s+/(\s|$)|update-alternatives|apt(-get)?\s+(install|remove|purge|upgrade|dist-upgrade)|dpkg\s+-(i|r|P)|hostnamectl|timedatectl\s+set)(?![\w-])',
     'This command changes system settings'),
    ('system', r'(>>?|tee(\s+-a)?|cp\s|mv\s|sed\s+-i|rm\s)[^|;&]*\s/etc/', 'This command writes to system settings under /etc'),
]
# (zone, command regex, reason): denied outright, with the fix in the reason. Kills that match by command line or
# process name can hit lamellar itself: the task text is on its command line and it runs as python3. (11-18: a model
# ran pkill -f "web.py --port 8767", the task mentioned that string, and the agent killed itself 40 minutes in.)
_PF = r'[^;&|\n]*?\s-\w*f(?![\w-])'   # a -f flag anywhere in the same command, alone or bundled (-f, -9f, -u me -f)
_DENY_ZONES = [
    ('self-kill', r'(?<![\w/.-])(pkill' + _PF + r'|killall|pgrep' + _PF + r'[^;&]*\|\s*xargs\s+(-\S+\s+)*kill|kill\s[^;&|]*\$\(\s*pgrep' + _PF + r')(?![\w-])',
     'pkill -f, killall and kill $(pgrep -f ...) match by command line or process name and can kill lamellar itself '
     '(your task text is on its command line). Stop a process by its PID instead: start it with '
     '"python3 web.py --port 8767 > web.log 2>&1 & echo $! > .web.pid" and stop it with "kill $(cat .web.pid)", '
     'or free a port with "fuser -k 8767/tcp".'),
]
# (zone, path regex, reason): for the file-write tools (writes are already confined to the working directory; this guards sensitive files inside it)
_PATH_ZONES = [
    ('credentials', r'(^|/)(\.htpasswd|authorized_keys|id_(rsa|ed25519|ecdsa)(\.pub)?|\.netrc|\.pgpass)$', 'This is a credentials file'),
    ('scheduling', r'(^|/)(crontab|cron\.d/.*)$', 'This is a scheduling file'),
]


_LOOKUP = re.compile(r'\b(command\s+-v|which|type|whereis|man|apropos)\s+[\w./-]+|[\w./-]+\s+(--help|--version|-V)\b')


class Guard:
    def __init__(self, cfg):
        self.on = cfg.get('guard_defaults', True)
        self.extra = cfg.get('guard_rules') or []      # [{tool, pattern, action: deny|ask|rewrite|allow, replace, reason}]

    def check(self, name, args):
        """Return (action, data). action: allow / deny / ask / rewrite.
        deny, ask: data is the reason text. rewrite: data is (new args, reason)."""
        subject = args.get('command', '') if name == 'bash' else str(args.get('path', ''))
        for r in self.extra:
            if r.get('tool') not in (None, '', '*', name): continue
            try: m = re.search(r['pattern'], subject)
            except re.error: continue
            if not m: continue
            act = r.get('action', 'deny'); why = r.get('reason') or 'Not allowed by a configured rule'
            if act == 'allow': return 'allow', None
            if act == 'rewrite':
                key = 'command' if name == 'bash' else 'path'
                return 'rewrite', ({**args, key: re.sub(r['pattern'], r.get('replace', ''), subject)}, why)
            return act, why
        if not self.on: return 'allow', None
        if name == 'bash':
            probe = _LOOKUP.sub(' ', subject)          # pure lookups (command -v, which, type, --help, --version) do not count
            for zone, pat, why in _DENY_ZONES:
                if re.search(pat, probe): return 'deny', f'[{zone}] {why}'
            for zone, pat, why in _CMD_ZONES:
                if re.search(pat, probe): return 'ask', f'[{zone}] {why}'
        elif name in ('write_file', 'edit_file'):
            for zone, pat, why in _PATH_ZONES:
                if re.search(pat, os.path.normpath(subject)): return 'ask', f'[{zone}] {why}'
        return 'allow', None
