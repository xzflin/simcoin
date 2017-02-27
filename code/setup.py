#!/usr/bin/env python
# -*- coding: utf-8 -*-
import sys
import os

if sys.version_info <= (3, 0):
    print("Sorry, requires Python 3.x or above")
    sys.exit(1)

# IP range from RFC6890 - IP range for future use
# it does not conflict with https://github.com/bitcoin/bitcoin/blob/master/src/netbase.h
ip_range = "240.0.0.0/4"
ip_bootstrap = "240.0.0.2"

image = 'btn/base:v3'
container_prefix = 'btn-'


class DataDir:

    @staticmethod
    def rootDir():
        return '$PWD/../data'

    @staticmethod
    def host(container_id):
        return DataDir.rootDir() + '/' + container_id

    @staticmethod
    def guest():
        return '/data'

    @staticmethod
    def log_file():
        """return the path to the execution log"""
        return '$PWD/../data/log'


class Docker:

    def __init__(self,plan):
        self.plan = plan

    def __enter__(self):
        self.plan.append('docker network create --subnet=' + ip_range + ' --driver bridge isolated_network ; sleep 1')
        return self

    def __exit__(self, excpetion_type, exception_value, traceback):
        self.plan.append('docker network rm isolated_network')

    @staticmethod
    def dockerBootstrapCmd (cmd):
        return (' '
                ' docker run '
                '   --detach=true '
                '   --net=isolated_network '
                '   --ip=' + ip_bootstrap + ' '
                '   --name=bootstrap'   # container name
                '   ' + image + ' '      # image name # src: https://hub.docker.com/r/abrkn/bitcoind/
                '   ' + cmd + ' '
                ' '
                )

    @staticmethod
    def dockerNodeCmd (name,cmd):
        return (' '
                ' docker run '
                '   --cap-add=NET_ADMIN ' # for `tc`
                '   --detach=true '
                '   --net=isolated_network '
                '   --name=' + name + ' '   # container name
                '   --hostname=' + name + ' '
                '   --volume ' + DataDir.host(name) + ':' + DataDir.guest() + ' '
                '   ' + image + ' '      # image name # src: https://hub.docker.com/r/abrkn/bitcoind/
                '   bash -c "' + cmd + '" '
                ' ')

    @staticmethod
    def dockerStp (name):
        return (' '
                ' docker rm --force ' + name + ' & '
                ' ')

    @staticmethod
    def cli(node,command):
        return (' '
                ' docker exec '
                + node +
                ' /bin/sh -c \''
                '    bitcoin-cli -regtest -datadir=' + DataDir.guest() + ' ' # -printtoconsole -daemon
                +    command +
                ' \' '
                ' ')

def bitcoindCmd (strategy = 'default'):
    daemon = ' bitcoind '
    default = {
          'regtest': ' -regtest ',             # activate regtest mode
          'datadir': ' -datadir=' + DataDir.guest() + ' ',       # change the datadir
          'debug': ' -debug ',                 # log all events
          # 'printblocktree': ' -printblocktree', # removed (would print tree on startup)
          # 'printtoconsole': ' -printtoconsole ', # print the log to stdout instead of a file
          'logips': ' -logips ',               # enable ip loging
          'logtimemicros': ' -logtimemicros',  # add microseconds to logging flag: DEFAULT_LOGTIMEMICROS
          'listen' : ' -listen ',              # ensure listening even if 'connect' is given
          'listenonion' : ' -listenonion=0 ',  # disable tor
          'onlynet': ' -onlynet=ipv4 ',        # disable ipv6
    }
    configs = {
        'default': {},
        'bootstrap' : {
            'disablewallet': ' -disablewallet=1 ' # disable wallet
        },
        'user': {
            'dnsseed' : ' -dnsseed=0 ',  # disable dns seed lookups, otherwise this gets seeds even with docker --internal network
            'addnode' : ' -addnode=' + ip_bootstrap + ' ', # only connect ourself introductionary node
            'seednode': ' -seednode=240.0.0.3 ',
            'keypool' : ' -keypool=1 '
        },
        'miner-solo' : {
            'addnode' : ' -addnode=fst ', # only connect to ourself introductionary node
            'keypool' : ' -keypool=1 '
        }
    }
    default.update(configs[strategy])
    return daemon + (' '.join(default.values()))


def nodeInfo(node):
    commands = [
        #        'getconnectioncount',
        #        'getblockcount',
        #        'getinfo',
        #        'getmininginfo',
                'getpeerinfo'
    ]
    return ';'.join([Docker.cli(node, cmd) for cmd in commands])


def slow_network(cmd):
    traffic_control = "tc qdisc replace dev eth0 root netem delay 100ms"
    return traffic_control + "; " + cmd
    # apt install iproute2
    # --cap-add=NET_ADMIN


class NodeManager():
    def __init__(self,plan,number_of_containers):
        self.ids = [ container_prefix + str(element) for element in range(number_of_containers)]
        self.nodes = [ Docker.dockerNodeCmd(id,slow_network(bitcoindCmd('user'))) for id in self.ids ]
        self.plan = plan

    def __enter__(self):
        self.plan.append( Docker.dockerBootstrapCmd(slow_network(bitcoindCmd('user'))) )
        self.plan.extend( self.nodes )
        self.plan.append('sleep 2') # wait before generating otherwise "Error -28" (still warming up)
        return self

    def __exit__(self, excpetion_type, exception_value, traceback):
        self.plan.extend( [ Docker.dockerStp(id) for id in self.ids] )
        self.plan.append( Docker.dockerStp('bootstrap') )
        self.plan.append( 'sleep 5')

    def randomNode(self):
        import random
        return random.choice(self.ids)

    def every_node_p(self, cmd):
        return [Docker.cli(_id, cmd) for _id in self.ids]

    def warmupBlockGeneration(self):
        # one block for each node
        # plus 100 blocks to enable spending
        return ['echo Begin of warmup'] + self.every_node_p('generate 1') + [self.randomBlockCommand(100)] + ['sleep 10']

    def randomBlockCommand(self, number=1):
        return Docker.cli(self.randomNode(), 'generate ' + str(number))

    def randomTransactionCommand(self):
        node = self.randomNode()
        return Docker.cli(node, 'sendtoaddress $(bitcoin-cli -regtest -datadir=' + DataDir.guest() + ' getnewaddress) 10.0')

    def log_chaintips(self):
        return self.every_node_p('getchaintips > ' + DataDir.guest() + '/chaintips.json')


def executionPlan(nodes, number_of_blocks):
    plan = []
    with Docker(plan):
        with NodeManager(plan, nodes) as nodeManager:
            os.system("rm -rf " + DataDir.host('*'))

            plan.extend(nodeManager.warmupBlockGeneration())

            import sys
            sys.path.append('./btn/src')
            from scheduler import Scheduler
            s = Scheduler()
            s.addblocks(4, [nodeManager.randomBlockCommand() for _ in range(130)])
            s.addtransactions(60, [nodeManager.randomTransactionCommand() for _ in range(10)])
            plan.extend(s.bash_commands().split('\n'))
            plan.extend(nodeManager.log_chaintips())

            plan.append('sleep 10')  # wait for blocks to spread

            plan.append('docker run --rm --volume ' + DataDir.rootDir() + ':/mnt' + ' ' + image + ' chmod a+rwx --recursive /mnt') # fix permissions on datadirs

            plan.extend(aggregate_logs(nodeManager.ids))

    return plan


def aggregate_logs(ids):
    commands = []
    timestamp_length = str(len('2016-09-22 14:46:41.706605'))

    def prefix_lines(prefix):
        return 'sed -e \'s/^/' + prefix + ' /\' '

    def remove_empty_lines():
        return 'sed ":a;N;$!ba;s/^\n/ /g" file'

    def remove_lines_starting_with_whitspace():
        return 'sed "s/^\s.*$//g"'

    def remove_multiline_error_messages():
        return 'sed "s/^.\{26\}  .*$//g"'

    def sed_command(_id): # insert node id after timestamp
        return 'sed "s/^.\{' + timestamp_length + '\}/& ' + _id + '/g"'

    "aggregate bitcoind logs"
    commands.append('rm -rf ' + DataDir.log_file())
    commands.extend([' cat ' + DataDir.host(_id) + '/regtest/debug.log | ' + sed_command(_id) + ' >> ' + DataDir.log_file() + '; ' for _id in ids])
    commands.append(' sort ' + DataDir.log_file())

    "aggregate fork information"
    commands.extend([' cat ' + DataDir.host(_id) + '/chaintips.json | jq "length" | ' + prefix_lines(_id) + '  >> ' + DataDir.rootDir() + '/forks; ' for _id in ids])

    return commands