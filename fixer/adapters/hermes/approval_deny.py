"""生成与线上审批规则相同顺序的禁止列表。"""


def approval_deny(config):
    deploy = config['fixer']['deploy']
    options = config['fixer']['approvals']
    deny = [
        '*docker push*', '*--push*', '*buildx rm*', '*buildx prune*',
        '*' + deploy['ci_builder_pattern'], '*' + deploy['ci_builder_container_pattern'] + '*',
        '*systemctl restart*', '*systemctl stop*', '*systemctl disable*', '*systemctl kill*',
        '*service * restart*', '*service * stop*', '*docker restart*', '*docker stop*',
        '*docker kill*', '*docker rm *', '*docker system prune*', '*docker container prune*',
        '*k3s-killall*', '*k3s-uninstall*', '*/etc/docker/*', '*/etc/rancher/*',
        '*/etc/containerd/*', '*/etc/systemd/*', '*kubectl delete namespace*',
        '*kubectl delete ns *', '*kubectl delete*--all-namespaces*', '*kubectl delete*-a *',
    ]
    deny += ['*-n ' + name + '*' for name in deploy['forbidden_namespaces']]
    deny += ['*--namespace ' + name + '*' for name in options['namespace_long_deny']]
    deny += ['*git push*--force*', '*git push* -f *']
    for index, branch in enumerate(options['push_deny_branches']):
        deny.append('*git push*origin ' + branch + '*')
        if index == 0:
            deny.append('*git push*:' + branch + '*')
    deny += ['*git push*origin ' + prefix + '*' for prefix in options['push_deny_prefixes']]
    return deny
