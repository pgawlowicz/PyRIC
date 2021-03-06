#!/usr/bin/env python

""" pyw.py: python iw

Copyright (C) 2016  Dale V. Patterson (wraith.wireless@yandex.com)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

Redistribution and use in source and binary forms, with or without
modifications, are permitted provided that the following conditions are met:
 o Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
 o Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
 o Neither the name of the orginal author Dale V. Patterson nor the names of
    any contributors may be used to endorse or promote products derived from
    this software without specific prior written permission.

Provides a python version of a subset of the iw command & additionally, a
smaller subset of ifconfig/iwconfig.

Each command/function (excluding interfaces & isinterface which do not rely on
ioctl/netlink sockets) comes in two flavors - one-time & persistent.
 1) one-time: similar to iw. The command, creates the netlink socket
    (or ioctl), composes the message, sends the message & receives the
    response, parses the results, closes the socket & returns the results to
    the caller. At no time does the caller need to be aware of any underlying
    netlink processes or structures.
 2) persistent: communication & parsing only. The onus of socket creation and
    deletion is on the caller which allows them to create one (or more)
    socket(s). The pyw functions will only handle message construction, message
    sending and receiving & message parsing.

Callers that intend to use pyw functionality often & repeatedly may prefer to
use a persistent netlink/ioctl socket. Socket creation & deletion are
relatively fast however, if a program is repeatedly using pyw function(s)
(such as a scanner that is changing channels mulitple times per second) it
makes sense for the caller to create a socket one time only & use the same
socket. However, if the caller is only using pyw periodically and/or does not
want to bothered with socket maintenance, the one-time flavor would be better.

for one-time execution, for example use

regset('US')

for persistent execution, use

regset('US',nlsocket)

where nlsocket is created with libnl.nl_socket_alloc()

NOTE:
 1) All functions (excluding wireless core related) will use a Card object which
    collates the physical index, device name and interface index (ifindex) in a
    tuple rather than a device name or physical index or ifindex as this will not
    require the caller to remember if a dev or a phy or a ifindex is needed. The
    Exceptions to this are:
     devinfo which will accept a Card or a dev
     devadd which will accept a Card or a phy
 2) All functions allow pyric errors to pass through. Callers must catch these
    if they desire

"""

__name__ = 'pyw'
__license__ = 'GPLv3'
__version__ = '0.1.8'
__date__ = 'July 2016'
__author__ = 'Dale Patterson'
__maintainer__ = 'Dale Patterson'
__email__ = 'wraith.wireless@yandex.com'
__status__ = 'Production'

import struct                                   # ioctl unpacking
import pyric                                    # pyric exception
import re                                       # check addr validity
from pyric.nlhelp.nlsearch import cmdbynum      # get command name
import pyric.utils.channels as channels         # channel related
import pyric.utils.rfkill as rfkill             # block/unblock
import pyric.utils.hardware as hw               # device related
import pyric.utils.ouifetch as ouifetch         # get oui dict
import pyric.net.netlink_h as nlh               # netlink definition
import pyric.net.genetlink_h as genlh           # genetlink definition
import pyric.net.wireless.nl80211_h as nl80211h # nl80211 definition
import pyric.net.wireless.wlan as wlan          # IEEE 802.11 Std definition
import pyric.net.sockios_h as sioch             # sockios constants
import pyric.net.if_h as ifh                    # ifreq structure
import pyric.lib.libnl as nl                    # netlink functions
import pyric.lib.libio as io                    # ioctl functions
import sys
_python3 = sys.version_info.major == 3

_FAM80211ID_ = None

# redefine some nl80211 enum lists for ease of use
IFTYPES = nl80211h.NL80211_IFTYPES
MNTRFLAGS = nl80211h.NL80211_MNTR_FLAGS
TXPWRSETTINGS = nl80211h.NL80211_TX_POWER_SETTINGS

################################################################################
#### WIRELESS CORE                                                          ####
################################################################################

def interfaces():
    """
     retrieves all network interfaces (APX ifconfig)
     :returns: a list of device names of current network interfaces cards
    """
    fin = None
    try:
        # read in devices from /proc/net/dev. After splitting on newlines, the
        # first 2 lines are headers and the last line is empty so we remove them
        fin = open(hw.dpath, 'r')
        ds = fin.read().split('\n')[2:-1]
    except IOError:
        return []
    finally:
        if fin: fin.close()

    # the remaining lines are <dev>: p1 p2 ... p3, split on ':' & strip whitespace
    return [d.split(':')[0].strip() for d in ds]

def isinterface(dev):
    """
     determines if device name belongs to a network card (APX ifconfig <dev>)
     :param dev: device name
     :returns: {True if dev is a device|False otherwise}
    """
    return dev in interfaces()

def winterfaces(*argv):
    """
     retrieve all wireless interfaces (APX iwconfig)
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: list of device names of current wireless NICs
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(winterfaces)

    wifaces = []
    for dev in interfaces():
        # no errors are caught here - but allowed to pass
        if iswireless(dev, iosock): wifaces.append(dev)
    return wifaces

def iswireless(dev, *argv):
    """
     determines if given device is wireless (APX iwconfig <dev>)
     :param dev: device name
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: {True:device is wireless|False:device is not wireless/not present}
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(iswireless, dev)

    try:
        # if the call succeeds, found to be wireless
        _ = io.io_transfer(iosock, sioch.SIOCGIWNAME, ifh.ifreq(dev))
        return True
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except io.error as e:
        # ENODEV or ENOTSUPP means not wireless, reraise any others
        if e.errno == pyric.ENODEV or e.errno == pyric.EOPNOTSUPP: return False
        else: raise pyric.error(e.errno, e.strerror)

def phylist():
    """ :returns: a list of tuples t = (physical indexe, physical name) """
    # we could walk the directory /sys/class/ieee80211 as well but we'll
    # let rfkill do it (just in case the above path differs across distros or
    # in future upgrades
    phys = []
    rfdevs = rfkill.rfkill_list()
    for rfk in rfdevs:
        if rfdevs[rfk]['type'] == 'wlan':
            phys.append((int(rfk.split('phy')[1]),rfk))
    return phys

def regget(*argv):
    """
     gets the current regulatory domain (iw reg get)
     :param argv: netlink socket at argv[0] (or empty)
     :returns: the two charactor regulatory domain
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(regget)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_REG,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)
    return nl.nla_find(rmsg, nl80211h.NL80211_ATTR_REG_ALPHA2)

def regset(rd, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the current regulatory domain (iw reg set <rd>)
     :param rd: regulatory domain code
     :param argv: netlink socket at argv[0] (or empty)
    """
    if len(rd) != 2:
        raise pyric.error(pyric.EINVAL, "Invalid reg. domain {0}".format(rd))
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(regset, rd)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_REQ_SET_REG,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_string(msg, rd.upper(), nl80211h.NL80211_ATTR_REG_ALPHA2)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

################################################################################
#### CARD RELATED ####
################################################################################

class Card(tuple):
    """
     A wireless network interface controller - Wrapper around a tuple
      t = (physical index,device name, interface index)
     Exposes the following properties: (callable by '.'):
      phy: physical index
      dev: device name
      idx: interface index (ifindex)
    """
    def __new__(cls, p, d, i):
        return super(Card, cls).__new__(cls, tuple((p, d, i)))
    def __repr__(self):
        return "Card(phy={0},dev={1},ifindex={2})".format(self.phy,
                                                          self.dev,
                                                          self.idx)
    @property
    def phy(self): return self[0]
    @property
    def dev(self): return self[1]
    @property
    def idx(self): return self[2]

def getcard(dev, *argv):
    """
     get the Card object from device name
     :param dev: device name
     :returns: a Card with device name dev
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(getcard, dev)

    return devinfo(dev, nlsock)['card']

def validcard(card, *argv):
    """
     determines if card is still valid i.e. another program has not changed it
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: True if card is still valid, False otherwise
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(validcard, card)

    try:
        return card == devinfo(card.dev, nlsock)['card']
    except pyric.error as e:
        if e.errno == pyric.ENODEV: return False
        else: raise

################################################################################
#### ADDRESS RELATED                                                        ####
################################################################################

def macget(card, *argv):
    """
     gets the interface's hw address (APX ifconfig <card.dev> | grep HWaddr)
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: device mac after operation
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(macget, card)

    try:
        flag = sioch.SIOCGIFHWADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam in [ifh.ARPHRD_ETHER, ifh.AF_UNSPEC,ifh.ARPHRD_IEEE80211_RADIOTAP]:
            return _hex2mac_(ret[18:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return hwaddr family")
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def macset(card, mac, *argv):
    """
     REQUIRES ROOT PRIVILEGES/CARD DOWN
     set nic's hwaddr (ifconfig <card.dev> hw ether <mac>)
     :param card: Card object
     :param mac: macaddr to set
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: mac address after operation
    """
    if  not _validmac_(mac): raise pyric.error(pyric.EINVAL, "Invalid mac address")

    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(macset, card, mac)

    try:
        flag = sioch.SIOCSIFHWADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag, [mac]))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam in [ifh.ARPHRD_ETHER, ifh.AF_UNSPEC, ifh.ARPHRD_IEEE80211_RADIOTAP]:
            return _hex2mac_(ret[18:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return hwaddr family")
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def inetget(card, *argv):
    """
     get nic's ip, netmask and broadcast addresses
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: the tuple t = (ip4,netmask,broadcast)
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(inetget, card)

    try:
        # ip
        flag = sioch.SIOCGIFADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            ip4 = _hex2ip4_(ret[20:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return ip family")

        # netmask
        flag = sioch.SIOCGIFNETMASK
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            netmask = _hex2ip4_(ret[20:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return netmask family")

        # broadcast
        flag = sioch.SIOCGIFBRDADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            brdaddr = _hex2ip4_(ret[20:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return broadcast family")
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        # catch address not available, which means the card currently does not
        # have any addresses set - raise others
        if e.errno == pyric.EADDRNOTAVAIL: return None, None, None
        raise pyric.error(e.errno, e.strerror)

    return ip4, netmask, brdaddr

def inetset(card, ipaddr, netmask, broadcast, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     set nic's ip4 addr, netmask and/or broadcast
      (ifconfig <card.dev> <ipaddr> netmask <netmask> broadcast <broadcast>)
     can set ipaddr,netmask and/or broadcast to None but one or more of ipaddr,
     netmask, broadcast must be set
     :param card: Card object
     :param ipaddr: ip address to set
     :param netmask: netmask to set
     :param broadcast: broadcast to set
     :param argv: ioctl socket at argv[0] (or empty)
     NOTE:
      1) throws error if setting netmask or broadcast and card does not have
       an ip assigned
      2) if setting only the ip address, netmask and broadcast will be set
         accordingly by the kernel.
      3) If setting multiple or setting the netmask and/or broadcast after the ip
         is assigned, one can set them to erroneous values i.e. ip = 192.168.1.2
         and broadcast = 10.0.0.31.
    """
    # ensure one of params is set & that all set params are valid ip address
    if not ipaddr and not netmask and not broadcast:
        raise pyric.error(pyric.EINVAL, "No parameters specified")
    if ipaddr and not _validip4_(ipaddr):
        raise pyric.error(pyric.EINVAL, "Invalid ip4 address")
    if netmask and not _validip4_(netmask):
        raise pyric.error(pyric.EINVAL, "Invalid netmask")
    if broadcast and not _validip4_(broadcast):
        raise pyric.error(pyric.EINVAL, "Invalid broadcast")

    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(inetset, card, ipaddr, netmask, broadcast)

    # we have to do one at a time
    try:
        # ip address first
        if ipaddr: ip4set(card, ipaddr, iosock)
        if netmask: netmaskset(card, netmask, iosock)
        if broadcast: broadcastset(card, broadcast, iosock)
    except pyric.error as e:
        # an ambiguous error is thrown if attempting to set netmask or broadcast
        # without an ip address already set on the card
        if not ipaddr and e.errno == pyric.EADDRNOTAVAIL:
            raise pyric.error(pyric.EINVAL, "Set ip4 addr first")
        else:
            raise
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))

def ip4set(card, ipaddr, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     set nic's ip4 addr  (ifconfig <card.dev> <ipaddr>
     :param card: Card object
     :param ipaddr: ip address to set
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: the new ip address
     NOTE: setting the ip will set netmask and broadcast accordingly
    """
    if not _validip4_(ipaddr): raise pyric.error(pyric.EINVAL, "Invalid ipaddr")

    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(ip4set, card, ipaddr)

    try:
        flag = sioch.SIOCSIFADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag, [ipaddr]))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            return _hex2ip4_(ipaddr)
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return ip family")
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def netmaskset(card, netmask, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     set nic's ip4 netmask (ifconfig <card.dev> netmask <netmask>
     :param card: Card object
     :param netmask: netmask to set
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: the new netmask
     NOTE:
      1) throws error if netmask is set and card does not have an ip assigned
    """
    if not _validip4_(netmask): raise pyric.error(pyric.EINVAL, "Invalid netmask")
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(netmaskset, card, netmask)

    try:
        flag = sioch.SIOCGIFNETMASK
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            return _hex2ip4_(ret[20:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return netmask family")
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        # an ambiguous error is thrown if attempting to set netmask or broadcast
        # without an ip address already set on the card
        if e.errno == pyric.EADDRNOTAVAIL:
            raise pyric.error(pyric.EINVAL, "Cannot set netmask. Set ip first")
        else:
            raise pyric.error(e, e.strerror)

def broadcastset(card, broadcast, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     set nic's ip4 netmask (ifconfig <card.dev> broadcast <broadcast>
     :param card: Card object
     :param broadcast: netmask to set
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: the new broadcast address
     NOTE:
      1) throws error if netmask is set and card does not have an ip assigned
      2) can set broadcast to erroneous values i.e. ipaddr = 192.168.1.2 and
      broadcast = 10.0.0.31.
    """
    if not _validip4_(broadcast):  raise pyric.error(pyric.EINVAL, "Invalid bcast")

    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(broadcastset, card, broadcast)

    # we have to do one at a time
    try:
        flag = sioch.SIOCGIFBRDADDR
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        fam = struct.unpack_from(ifh.sa_addr, ret, ifh.IFNAMELEN)[0]
        if fam == ifh.AF_INET:
            return _hex2ip4_(ret[20:24])
        else:
            raise pyric.error(pyric.EAFNOSUPPORT, "Invalid return broadcast family")
    except pyric.error as e:
        # an ambiguous error is thrown if attempting to set netmask or broadcast
        # without an ip address already set on the card
        if e.errno == pyric.EADDRNOTAVAIL:
            raise pyric.error(pyric.EINVAL, "Cannot set broadcast. Set ip first")
        else:
            raise
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        # an ambiguous error is thrown if attempting to set netmask or broadcast
        # without an ip address already set on the card
        if e.errno == pyric.EADDRNOTAVAIL:
            raise pyric.error(pyric.EINVAL, "Cannot set broadcast. Set ip first")
        else:
            raise pyric.error(e, e.strerror)

################################################################################
#### HARDWARE ON/OFF                                                        ####
################################################################################

def isup(card, *argv):
    """
     determine on/off state of card
     :param card: Card object
     :param argv: ioctl socet at argv[0] (or empty)
     :returns: True if card is up, False otherwise
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(isup, card)

    try:
        return _issetf_(_flagsget_(card.dev, iosock), ifh.IFF_UP)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")

def up(card, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     turns dev on (ifconfig <card.dev> up)
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(up, card)

    try:
        flags = _flagsget_(card.dev, iosock)
        if not _issetf_(flags, ifh.IFF_UP):
            _flagsset_(card.dev, _setf_(flags, ifh.IFF_UP), iosock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")

def down(card, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     turns def off (ifconfig <card.dev> down)
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(down, card)

    try:
        flags = _flagsget_(card.dev, iosock)
        if _issetf_(flags, ifh.IFF_UP):
            _flagsset_(card.dev, _unsetf_(flags, ifh.IFF_UP), iosock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")

def isblocked(card):
    """
     determines blocked state of Card
     :param card: Card object
     :returns: tuple (Soft={True if soft blocked|False otherwise},
                      Hard={True if hard blocked|False otherwise})
    """
    try:
        idx = rfkill.getidx(card.phy)
        return rfkill.soft_blocked(idx), rfkill.hard_blocked(idx)
    except AttributeError:
        raise pyric.error(pyric.ENODEV, "Card is no longer regsitered")

def block(card):
    """
     soft blocks card
     :param card: Card object
    """
    try:
        idx = rfkill.getidx(card.phy)
        rfkill.rfkill_block(idx)
    except AttributeError:
        raise pyric.error(pyric.ENODEV, "Card is no longer registered")

def unblock(card):
    """
     turns off soft block
     :param card:
    """
    try:
        idx = rfkill.getidx(card.phy)
        rfkill.rfkill_unblock(idx)
    except AttributeError:
        raise pyric.error(pyric.ENODEV, "Card no longer registered")

################################################################################
#### RADIO PROPERTIES                                                       ####
################################################################################

def pwrsaveget(card, *argv):
    """
     returns card's power save state
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: True if power save is on, False otherwise
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(pwrsaveget, card)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_POWER_SAVE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    return nl.nla_find(rmsg, nl80211h.NL80211_ATTR_PS_STATE) == 1

def pwrsaveset(card, on, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets card's power save state
     :param card: Card object
     :param on: {True = on|False = off}
     :param argv: netlink socket at argv[0] (or empty)
     sets card's power save
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(pwrsaveset, card, on)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_POWER_SAVE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nla_put_u32(msg, int(on), nl80211h.NL80211_ATTR_PS_STATE)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except ValueError:
        raise pyric.error(pyric.EINVAL, "Invalid parameter on")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def covclassget(card, *argv):
    """
     gets the coverage class value
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: coverage class value
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(covclassget, card)

    return phyinfo(card, nlsock)['cov_class']

def covclassset(card, cc, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the coverage class. The coverage class IAW IEEE Std 802.11-2012 is
     defined as the Air propagation time & together with max tx power control
     the BSS diamter
     :param card: Card object
     :param cc: coverage class 0 to 31 IAW IEEE Std 802.11-2012 Table 8-56
     :param argv: netlink socket at argv[0] (or empty)
     sets card's coverage class
    """
    if cc < wlan.COV_CLASS_MIN or cc > wlan.COV_CLASS_MAX:
        # this can work 'incorrectly' on non-int values but these will
        # be caught later during conversion
        msg = "Cov class must be integer {0}-{1}".format(wlan.COV_CLASS_MIN,
                                                         wlan.COV_CLASS_MAX)
        raise pyric.error(pyric.EINVAL, msg)

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(covclassset, card, cc)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u8(msg, int(cc), nl80211h.NL80211_ATTR_WIPHY_COVERAGE_CLASS)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except ValueError:
        raise pyric.error(pyric.EINVAL, "Invalid value for Cov. Class")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def retryshortget(card, *argv):
    """
     gets the short retry limit.
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     gets card's short retry
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(retryshortget, card)

    return phyinfo(card, nlsock)['retry_short']

def retryshortset(card, lim, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the short retry limit.
     :param card: Card object
     :param lim: max # of short retries 1 - 255
     :param argv: netlink socket at argv[0] (or empty)
     sets card's shorty retry
    """
    if lim < wlan.RETRY_MIN or lim > wlan.RETRY_MAX:
        # this can work 'incorrectly' on non-int values but these will
        # be caught later during conversion
        msg = "Retry short must be integer {0}-{1}".format(wlan.RETRY_MIN,
                                                           wlan.RETRY_MAX)
        raise pyric.error(pyric.EINVAL, msg)

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(retryshortset, card, lim)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u8(msg, int(lim), nl80211h.NL80211_ATTR_WIPHY_RETRY_SHORT)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except ValueError:
        raise pyric.error(pyric.EINVAL, "Invalid parameter value for lim")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def retrylongget(card, *argv):
    """
     gets the long retry limit.
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     gets card's long retry
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(retrylongget, card)

    return phyinfo(card, nlsock)['retry_long']

def retrylongset(card, lim, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the long retry limit.
     :param card: Card object
     :param lim: max # of short retries 1 - 255
     :param argv: netlink socket at argv[0] (or empty)
     sets card's long retry
    """
    if lim < wlan.RETRY_MIN or lim > wlan.RETRY_MAX:
        # this can work 'incorrectly' on non-int values but these will
        # be caught later during conversion
        msg = "Retry long must be integer {0}-{1}".format(wlan.RETRY_MIN,
                                                          wlan.RETRY_MAX)
        raise pyric.error(pyric.EINVAL, msg)

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(retrylongset, card, lim)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u8(msg, int(lim), nl80211h.NL80211_ATTR_WIPHY_RETRY_LONG)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except ValueError:
        raise pyric.error(pyric.EINVAL, "Invalid parameter value for lim")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def rtsthreshget(card, *argv):
    """
     gets RTS Threshold
     :param card: Card Object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: RTS threshold
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(rtsthreshget, card)

    return phyinfo(card, nlsock)['rts_thresh']

def rtsthreshset(card, thresh, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the RTS threshold. If off, RTS is disabled. If an integer, sets the
     smallest packet for which card will send an RTS prior to each transmission
     :param card: Card object
     :param thresh: rts threshold limit
     :param argv: netlink socket at argv[0] (or empty)
     sets the card's RTS threshold
    """
    if thresh == 'off': thresh = wlan.RTS_THRESH_OFF
    elif thresh == wlan.RTS_THRESH_OFF: pass
    elif thresh == 'on':
        msg = "Thresh must be 'off' or integer {0}-{1}".format(wlan.RTS_THRESH_MIN,
                                                               wlan.RTS_THRESH_MAX)
        raise pyric.error(pyric.EINVAL, msg)
    elif thresh < wlan.RTS_THRESH_MIN or thresh > wlan.RTS_THRESH_MAX:
        msg = "Thresh must be 'off' or integer {0}-{1}".format(wlan.RTS_THRESH_MIN,
                                                               wlan.RTS_THRESH_MAX)
        raise pyric.error(pyric.EINVAL, msg)

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(rtsthreshset, card, thresh)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u32(msg, thresh, nl80211h.NL80211_ATTR_WIPHY_RTS_THRESHOLD)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except ValueError:
        raise pyric.error(pyric.EINVAL, "Invalid parameter value for thresh")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def fragthreshget(card, *argv):
    """
     gets Fragmentation Threshold
     :param card: Card Object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: RTS threshold
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(fragthreshget, card)

    return phyinfo(card, nlsock)['frag_thresh']

def fragthreshset(card, thresh, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets the Frag threshold. If off, fragmentation is disabled. If an integer,
     sets the largest packet before the card will enable fragmentation
     :param card: Card object
     :param thresh: frag threshold limit in octets
     :param argv: netlink socket at argv[0] (or empty)
     sets the card's Fragmentation THRESH
    """
    if thresh == 'off': thresh = wlan.FRAG_THRESH_OFF
    elif thresh == wlan.FRAG_THRESH_OFF: pass
    elif thresh == "on":
        msg = "Thresh must be 'off' or integer {0}-{1}".format(wlan.FRAG_THRESH_MIN,
                                                               wlan.FRAG_THRESH_MAX)
        raise pyric.error(pyric.EINVAL, msg)
    elif thresh < wlan.FRAG_THRESH_MIN or thresh > wlan.FRAG_THRESH_MAX:
        msg = "Thresh must be 'off' or integer {0}-{1}".format(wlan.FRAG_THRESH_MIN,
                                                               wlan.FRAG_THRESH_MAX)
        raise pyric.error(pyric.EINVAL, msg)

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(fragthreshset, card, thresh)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u32(msg, thresh, nl80211h.NL80211_ATTR_WIPHY_FRAG_THRESHOLD)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

################################################################################
#### INFO RELATED                                                           ####
################################################################################

def devfreqs(card, *argv):
    """
     returns card's supported frequencies
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: list of supported frequencies
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devfreqs, card)

    return phyinfo(card, nlsock)['freqs']

def devchs(card, *argv):
    """
     returns card's supported channels
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: list of supported channels
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devchs, card)

    return list(map(channels.rf2ch, phyinfo(card, nlsock)['freqs']))

def devstds(card, *argv):
    """
     gets card's wireless standards (iwconfig <card.dev> | grep IEEE
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: list of standards (letter designators)
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devstds, card)

    stds = []
    bands = phyinfo(card,nlsock)['bands']
    if '5GHz' in bands: stds.append('a')
    if '2GHz' in bands: stds.extend(['b','g']) # assume backward compat with b
    HT = VHT = True
    for band in bands:
        HT &= bands[band]['HT']
        VHT &= bands[band]['VHT']
    if HT: stds.append('n')
    if VHT: stds.append('ac')
    return stds

def devmodes(card, *argv):
    """
     gets supported modes card can operate in
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: list of card's supported modes
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devmodes, card)

    return phyinfo(card, nlsock)['modes']

def devcmds(card, *argv):
    """
     get supported commands card can execute
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: supported commands
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devcmds, card)

    return phyinfo(card, nlsock)['commands']

def ifinfo(card, *argv):
    """
     get info for interface (ifconfig <dev>)
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: dict with the following key:value pairs
     driver -> card's driver
     chipset -> card's chipset
     manufacturer -> card's manufacturer
     hwaddr -> card's mac address
     inet -> card's inet address
     bcast -> card's broadcast address
     mask -> card's netmask address
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(ifinfo, card)

    # get oui dict
    ouis = {}
    try:
        ouis = ouifetch.load()
    except pyric.error:
        pass

    try:
        drvr, chips = hw.ifcard(card.dev)
        mac = macget(card, iosock)
        ip4, nmask, bcast = inetget(card, iosock)
        info = {'driver':drvr, 'chipset':chips, 'hwaddr':mac,
                'manufacturer':hw.manufacturer(ouis,mac),
                'inet':ip4, 'bcast':bcast, 'mask':nmask}
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")

    return info

def devinfo(card, *argv):
    """
     get info for device (iw dev <dev> info)
     :param card: Card object or dev
     :param argv: netlink socket at argv[0] (or empty)
     :returns: dict with the following key:value pairs
      card -> Card(phy,dev,ifindex)
      mode -> i.e. monitor or managed
      wdev -> wireless device id
      mac -> hw address
      RF (if associated) -> frequency
      CF (if assoicate) -> center frequency
      CHW -> channel width i.e. NOHT,HT40- etc
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devinfo, card)

    # if we have a Card, pull out dev name, ifindex itherwise get ifindex
    try:
        dev = card.dev
        idx = card.idx
    except AttributeError:
        dev = card
        idx = _ifindex_(dev)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_INTERFACE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    # pull out attributes
    info = {
        'card': Card(nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY), dev, idx),
        'mode': IFTYPES[nl.nla_find(rmsg, nl80211h.NL80211_ATTR_IFTYPE)],
        'wdev': nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WDEV),
        'mac': _hex2mac_(nl.nla_find(rmsg, nl80211h.NL80211_ATTR_MAC)),
        'RF': nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_FREQ),
        'CF': nl.nla_find(rmsg, nl80211h.NL80211_ATTR_CENTER_FREQ1),
        'CHW': nl.nla_find(rmsg, nl80211h.NL80211_ATTR_CHANNEL_WIDTH)
    }

    # convert CHW to string version
    try:
        info['CHW'] = channels.CHTYPES[info['CHW']]
    except (IndexError,TypeError): # invalid index and NoneType
        info['CHW'] = None
    return info

def phyinfo(card, *argv):
    """
     get info for phy (iw phy <phy> info)
     :param card: Card
     :param argv: netlink socket at argv[0] (or empty)
     :returns: dict with the following key:value pairs
      generation -> wiphy generation
      modes -> list of supported modes
      bands -> dict of supported bands of the form
       bandid -> {'rates': list of supported rates,
                  'rfs': list of supported freqs,
                  'rd-data': list of data corresponding to rfs,
                  'HT': 802.11n HT supported,
                  'VHT': 802.11ac VHT supported}
      scan_ssids -> max number of scan SSIDS
      retry_short -> retry short limit
      retry_long -> retry long limit
      frag_thresh -> frag threshold
      rts_thresh -> rts threshold
      cov_class -> coverage class
      swmodes -> supported software modes
      commands -> supported commands
      ciphers -> supported ciphers
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(phyinfo, card)

    # iw sends @NL80211_ATTR_SPLIT_WIPHY_DUMP, we don't & get full return at once
    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    # pull out attributes
    info = {
        'generation':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_GENERATION),
        'retry_short':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_RETRY_SHORT),
        'retry_long':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_RETRY_LONG),
        'frag_thresh':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_FRAG_THRESHOLD),
        'rts_thresh':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_RTS_THRESHOLD),
        'cov_class':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_COVERAGE_CLASS),
        'scan_ssids':nl.nla_find(rmsg, nl80211h.NL80211_ATTR_MAX_NUM_SCAN_SSIDS),
        'bands':[],
        'modes':[],
        'swmodes':[],
        'commands':[],
        'ciphers':[]
    }

    # modify frag_thresh and rts_thresh as necessary
    if info['frag_thresh'] >= wlan.FRAG_THRESH_MAX: info['frag_thresh'] = 'off'
    if info['rts_thresh'] >= wlan.RTS_THRESH_MAX: info['rts_thresh'] = 'off'

    # complex attributes
    # NOTE: after correcting my understanding of how to parsed nested attributes
    # they should no longer result in a NLA_ERROR but just in case...
    _, bs, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_WIPHY_BANDS, False)
    if d != nlh.NLA_ERROR: info['bands'] = _bands_(bs)

    _, cs, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_CIPHER_SUITES, False)
    if d != nlh.NLA_ERROR: info['ciphers'] = _ciphers_(cs)

    # supported iftypes, sw iftypes are IAW nl80211.h flags (no attribute data)
    _, ms, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_SUPPORTED_IFTYPES, False)
    if d != nlh.NLA_ERROR: info['modes'] = [_iftypes_(iftype) for iftype,_ in ms]

    _, ms, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_SOFTWARE_IFTYPES, False)
    if d != nlh.NLA_ERROR: info['swmodes'] = [_iftypes_(iftype) for iftype,_ in ms]

    # get supported commands
    _, cs, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_SUPPORTED_COMMANDS, False)
    if d != nlh.NLA_ERROR: info['commands'] = _commands_(cs)

    return info

################################################################################
#### TX/RX RELATED ####
################################################################################

def txset(card, setting, lvl, *argv):
    """
     ROOT Required
      sets cards tx power (iw phy card.<phy> <lvl> <pwr> * 100)
     :param card: Card object
     :param setting: power level setting oneof {'auto' = automatically determine
      transmit power|'limit' = limit power by <pwr>|'fixed' = set to <pwr>}
     :param lvl: desired tx power in dBm or None. NOTE: ignored if lvl is 'auto'
     :param argv: netlink socket at argv[0] (or empty)
     :returns: True on success
     NOTE: this does not work on my card(s) (nor does the corresponding iw
      command)
    """
    # sanity check on power setting and power level
    if not setting in TXPWRSETTINGS:
        raise pyric.error(pyric.EINVAL, "Invalid power setting {0}".format(setting))
    if setting != 'auto':
        if lvl is None:
            raise pyric.error(pyric.EINVAL, "Power level must be specified")

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(txset, card, setting, lvl)

    try:
        setting = TXPWRSETTINGS.index(setting)
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        # neither sending the phy or ifindex works
        #nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nla_put_u32(msg, setting, nl80211h.NL80211_ATTR_WIPHY_TX_POWER_SETTING)
        if setting != nl80211h.NL80211_TX_POWER_AUTOMATIC:
            nl.nla_put_u32(msg, 100*lvl, nl80211h.NL80211_ATTR_WIPHY_TX_POWER_LEVEL)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except ValueError:
        # converting to mBm
        raise pyric.error(pyric.EINVAL, "Invalid txpwr {0}".format(lvl))
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def txget(card, *argv):
    """
     gets card's transmission power (iwconfig <card.dev> | grep Tx-Power)
     :param card: Card object
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: transmission power in dBm
     info can be found by cat /sys/kernel/debug/ieee80211/phy<#>/power but
     how valid is it?
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(txget, card)

    try:
        flag = sioch.SIOCGIWTXPOW
        ret = io.io_transfer(iosock, flag, ifh.ifreq(card.dev, flag))
        return struct.unpack_from(ifh.ifr_iwtxpwr, ret, ifh.IFNAMELEN)[0]
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except IndexError:
        return None
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def chget(card, *argv):
    """
     gets the current channel for device (iw dev <card.dev> info | grep channel)
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     NOTE: will only work if dev is associated w/ AP or device is in monitor mode
     and has had chset previously
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(chget, card)

    # rf2ch will return None if Card is not on a channel
    return channels.rf2ch(devinfo(card, nlsock)['RF'])

def chset(card, ch, chw=None, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     sets current channel on device (iw phy <card.phy> set channel <ch> <chw>)
     :param card: Card object
     :param ch: channel number
     :param chw: channel width oneof {[None|'HT20'|'HT40-'|'HT40+'}
     :param argv: netlink socket at argv[0] (or empty)
     :returns: True on success
     NOTE:
      o Can throw a device busy for several reason. Most likely due to
       the network manager etc.
      o On my system at least (Ubuntu), creating a new dev in monitor mode and
        deleting all other existing managed interfaces allows for the new virtual
        device's channels to be changed w/out interference from network manager
    """
    if ch not in channels.channels():
        raise pyric.error(pyric.EINVAL, "Invalid channel")

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(chset, card, ch, chw)

    return freqset(card, channels.ch2rf(ch), chw, nlsock)

def freqset(card, rf, chw=None, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     set the frequency and width
     :param card: Card object
     :param rf: frequency
     :param chw: channel width oneof {[None|'HT20'|'HT40-'|'HT40+'}
     :param argv: netlink socket at argv[0] (or empty)
    """
    if rf not in channels.freqs(): raise pyric.error(pyric.EINVAL, "Invalid RF")
    if chw in channels.CHTYPES: chw = channels.CHTYPES.index(chw)
    else: raise pyric.error(pyric.EINVAL, "Invalid channel width")

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(freqset, card, rf, chw)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_WIPHY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_u32(msg, rf, nl80211h.NL80211_ATTR_WIPHY_FREQ)
        nl.nla_put_u32(msg, chw, nl80211h.NL80211_ATTR_WIPHY_CHANNEL_TYPE)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

#### INTERFACE & MODE RELATED ####

def modeget(card, *argv):
    """
     get current mode of card
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :return:
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(modeget, card)

    return devinfo(card, nlsock)['mode']

def modeset(card, mode, flags=None, *argv):
    """
     REQUIRES ROOT PRIVILEGES/CARD DOWN
     sets card to mode (with optional flags if mode is monitor)
     (APX iw dev <card.dev> set type <mode> [flags])
     NOTE: as far
     :param card: Card object
     :param mode: 'name' of mode to operate in (must be one of in {'unspecified'|
     'ibss'|'managed'|'AP'|'AP VLAN'|'wds'|'monitor'|'mesh'|'p2p'}
     :param flags: list of monitor flags (can only be used if card is being set
      to monitor mode) neof {'invalid'|'fcsfail'|'plcpfail'|'control'|'other bss'
                             |'cook'|'active'}
     :param argv: netlink socket at argv[0] (or empty)
    """
    if mode not in IFTYPES: raise pyric.error(pyric.EINVAL, 'Invalid mode')
    if flags:
        if mode != 'monitor':
            raise pyric.error(pyric.EINVAL, 'Can only set flags in monitor mode')
        for flag in flags:
            if flag not in MNTRFLAGS:
                raise pyric.error(pyric.EINVAL, 'Invalid flag: {0}'.format(flag))
    else: flags = []

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(modeset, card, mode, flags)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_SET_INTERFACE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nla_put_u32(msg, IFTYPES.index(mode), nl80211h.NL80211_ATTR_IFTYPE)
        for flag in flags:
            nl.nla_put_u32(msg,
                           MNTRFLAGS.index(flag),
                           nl80211h.NL80211_ATTR_MNTR_FLAGS)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def ifaces(card, *argv):
    """
     returns all interfaces sharing the same phy as card (APX iw dev | grep phy#)
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: a list of tuples t = (Card,mode) for each device having the same
      phyiscal index as that of card
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(ifaces, card)

    ifs = []
    for dev in winterfaces():
        info = devinfo(dev, nlsock)
        try:
            if info['card'].phy == card.phy:
                ifs.append((info['card'], info['mode']))
        except AttributeError:
            raise pyric.error(pyric.EINVAL, "Invalid Card")
        except nl.error as e:
            raise pyric.error(e.errno, e.strerror)
    return ifs

def devset(card, ndev, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     changes card's dev to ndev
     :param card: Card object
     :param ndev: new dev name
     :param argv: netlink socket at argv[0] (or empty)
     :returns: the new card object
     #NOTE:
      o via netlink one can set a new physical name but we want the ability to
        set a new dev.
      o this is not a true set name: it adds a new card with ndev as the dev then
        deletes the current card, returning the new card
       - in effect, it will appear as if the card as a new name but, it will also
         have a new ifindex
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devset, card, ndev)

    new = None # appease PyCharm
    try:
        new = devadd(card, ndev, modeget(card, nlsock), None, nlsock)
        devdel(card, nlsock)
    except pyric.error:
        # try and restore the system i.e. delete new if possible
        if new:
            try:
                devdel(new, nlsock)
            except pyric.error:
                pass
        raise
    return new

def devadd(card, vdev, mode, flags=None, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     adds a virtual interface on device having type mode (iw phy <card.phy>
      interface add <vnic> type <mode>
     :param card: Card object or physical index
     :param vdev: device name of new interface
     :param mode: 'name' of mode to operate in (must be one of in {'unspecified'|
     'ibss'|'managed'|'AP'|'AP VLAN'|'wds'|'monitor'|'mesh'|'p2p'}
     :param flags: list of monitor flags (can only be used if creating monitor
     mode) oneof {'invalid'|'fcsfail'|'plcpfail'|'control'|'other bss'
                  |'cook'|'active'}
     :param argv: netlink socket at argv[0] (or empty)
     :returns: the new Card
    """
    if mode not in IFTYPES: raise pyric.error(pyric.EINVAL, 'Invalid mode')
    if flags:
        if mode != 'monitor':
            raise pyric.error(pyric.EINVAL, 'Can only set flags in monitor mode')
        for flag in flags:
            if flag not in MNTRFLAGS:
                raise pyric.error(pyric.EINVAL, 'Invalid flag: {0}'.format(flag))
    else: flags = []

    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devadd, card, vdev, mode, flags)

    # if we have a Card, pull out phy index
    try:
        phy = card.phy
    except AttributeError:
        phy = card

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_NEW_INTERFACE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, phy, nl80211h.NL80211_ATTR_WIPHY)
        nl.nla_put_string(msg, vdev, nl80211h.NL80211_ATTR_IFNAME)
        nl.nla_put_u32(msg, IFTYPES.index(mode), nl80211h.NL80211_ATTR_IFTYPE)
        for flag in flags:
            nl.nla_put_u32(msg,
                           MNTRFLAGS.index(flag),
                           nl80211h.NL80211_ATTR_MNTR_FLAGS)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock) # success returns new device attributes
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    return Card(card.phy, vdev, nl.nla_find(rmsg, nl80211h.NL80211_ATTR_IFINDEX))

def devdel(card, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     deletes the device (dev <card.dev> del
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: True if success, False otherwise
     NOTE: the original card is no longer valid (i.e. the phy will still be present
     but the device name and ifindex are no longer 'present' in the system
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(devdel, card)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_DEL_INTERFACE,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
        return True
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)
    return False

################################################################################
#### STA FUNCTIONS                                                          ####
################################################################################

def isconnected(card, *argv):
    """
     disconnect the card from an AP
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(isconnected, card)

    # dirty hack - using the precence of an RF to determine connected-ness
    return devinfo(card, nlsock)['RF'] is not None

def disconnect(card, *argv):
    """
     REQUIRES ROOT PRIVILEGES
     disconnect the card from an AP
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     NOTE: does not return error if card is not connected
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(disconnect, card)

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_DISCONNECT,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nl_sendmsg(nlsock, msg)
        _ = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

def link(card, *argv):
    """
     returns info about link (iw dev card.<dev> link)
     :param card: Card object
     :param argv: netlink socket at argv[0] (or empty)
     :returns: link info as dict  with the following key:value pairs
       bssid -> AP mac/ net BSSID
       ssid -> the ssid (Experimental)
       freq -> BSSID frequency in MHz
       chw -> width of the BSS control channel
       rss -> Received signal strength in dBm
       int -> beacon interval (ms)
       stat -> status w.r.t of card to BSS one of {'authenticated','associated','ibss'}
       tx -> tx metrics dict of the form
        pkts -> total sent packets to connected STA (AP)
        bytes -> total sent in bytes to connected STA (AP)
        retries -> total # of retries
        failed -> total # of failed
        bitrate -> dict of form
          rate -> tx rate in Mbits
          width -> channel width oneof {None|20|40}
          mcs-index -> mcs index (0..32) or None
          gaurd -> guard interval oneof {None|0=short|1=long}
          Note: width, mcs-index, guard will be None unless 802.11n is being used
       rx -> rx metrics dict (see tx for format exluces retries and fails)
      or None if the card is not connected
     NOTE: if the nested attribute was not parsed correctly will attempt to pull
      out as much as possible
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(link, card)

    # if we're not connected GET_SCAN will dump scan results, we don't want that
    if not isconnected(card, nlsock): return None

    try:
        # we need to set additional flags or the kernel will return ERRNO 95
        flags = nlh.NLM_F_REQUEST | nlh.NLM_F_ACK | nlh.NLM_F_ROOT | nlh.NLM_F_MATCH
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_SCAN,
                           flags=flags)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    # link returns multiple attributes but we are only concerned w/ @NL80211_ATTR_BSS
    # some cards (my integrated intel) do not parse correctly
    info = {'bssid': None, 'ssid': None, 'freq': None, 'rss': None, 'int': None,
            'chw': None, 'stat': None,'tx': {}, 'rx': {}}

    _, bs, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_BSS, False)
    if d == nlh.NLA_ERROR: return info
    for idx, attr in bs:
        # any errors attempting to parse -> leave as default None, empty
        try:
            if idx == nl80211h.NL80211_BSS_BSSID:
                info['bssid'] = _hex2mac_(attr)
            if idx == nl80211h.NL80211_BSS_FREQUENCY:
                info['freq'] = struct.unpack_from('I', attr, 0)[0]
            if idx == nl80211h.NL80211_BSS_SIGNAL_MBM:
                info['rss'] = struct.unpack_from('i', attr, 0)[0] / 100
            if idx == nl80211h.NL80211_BSS_INFORMATION_ELEMENTS:
                # hacking the proprietary info element attribute: (it should
                # be a nested attribute itself, but I have currently no way of
                # knowing what the individual indexes would mean
                #    "\x06\x00\x00<l>SSID.....
                # '\x06\x00' is the ie index & the ssid is the first element
                # (from what I've seen). This is not nested. Not sure if the
                # length is the first two bytes or just the second  Get the length of the ssid which is the 3rd,4th byte, then unpack
                # the string starting at the fifth byte up to the length
                try:
                    l = struct.unpack_from('>H', attr, 0)[0] # have to change the format
                    info['ssid'] = struct.unpack_from('{0}s'.format(l), attr, 2)[0]
                except struct.error:
                    pass
            if idx == nl80211h.NL80211_BSS_BEACON_INTERVAL:
                info['int'] = struct.unpack_from('H', attr, 0)[0]
            if idx == nl80211h.NL80211_BSS_CHAN_WIDTH:
                j = struct.unpack_from('I', attr, 0)[0]
                info['chw'] = nl80211h.NL80211_BSS_CHAN_WIDTHS[j]
            if idx == nl80211h.NL80211_BSS_STATUS:
                j = struct.unpack_from('I', attr, 0)[0]
                info['stat'] = nl80211h.NL80211_BSS_STATUSES[j]
        except struct.error:
            pass

    # process stainfo of AP
    try:
        sinfo = stainfo(card, info['bssid'], nlsock)
        info['tx'] = {'bytes': sinfo['tx-bytes'],
                      'pkts': sinfo['tx-pkts'],
                      'failed': sinfo['tx-failed'],
                      'retries': sinfo['tx-retries'],
                      'bitrate': {'rate': sinfo['tx-bitrate']['rate']}}
        if 'mcs-index' in sinfo['tx-bitrate']:
            info['tx']['bitrate']['mcs-index'] = sinfo['tx-bitrate']['mcs-index']
            info['tx']['bitrate']['gi'] = sinfo['tx-bitrate']['gi']
            info['tx']['bitrate']['width'] = sinfo['tx-bitrate']['width']

        info['rx'] = {'bytes': sinfo['rx-bytes'],
                      'pkts':sinfo['rx-pkts'],
                      'bitrate': {'rate': sinfo['rx-bitrate']['rate']}}
        if 'mcs-index' in sinfo['rx-bitrate']:
            info['rx']['bitrate']['mcs-index'] = sinfo['rx-bitrate']['mcs-index']
            info['rx']['bitrate']['gi'] = sinfo['rx-bitrate']['gi']
            info['rx']['bitrate']['width'] = sinfo['rx-bitrate']['width']
    except (KeyError,TypeError,AttributeError):
        # ignore for now, returning what we got
        pass

    return info

def stainfo(card, mac, *argv):
    """
     returns info about sta (AP) the card is associated with (iw dev card.<dev> link)
     :param card: Card object
     :param mac: mac address of STA
     :param argv: netlink socket at argv[0] (or empty)
     :returns: sta info as dict  with the following key:value pairs
      rx-bytes: total received bytes (from STA)
      tx-bytes: total sent bytes (to STA)
      rx-pkts: total received packets (from STA)
      tx-pkts: total sent packets (to STA)
      tx-bitrate: dict of the form
       rate: bitrate in 100kbits/s
       legacy: fallback bitrate in 100kbits/s (only present if rate is not determined)
       mcs-index: mcs index (0..32) (only present if 802.11n)
       gi: guard interval oneof {0=short|1=long} (only present if 802.11n)
       width: channel width oneof {20|40}
      rx-bitrate: see tx-bitrate
     NOTE:
      - if the nested attribute was not parsed correctly will attempt to pull
       out as much as possible
      - given msc index, guard interval and channel width, one can calculate the
       802.11n rate (see wraith->standards->mcs)
    """
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(stainfo, card, mac)

    # if we're not connected GET_SCAN will dump scan results, we don't want that
    if not isconnected(card, nlsock): return None

    try:
        msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                           cmd=nl80211h.NL80211_CMD_GET_STATION,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
        nl.nla_put_unspec(msg, _mac2hex_(mac), nl80211h.NL80211_ATTR_MAC)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
    except AttributeError:
        raise pyric.error(pyric.EINVAL, "Invalid Card")
    except nl.error as e:
        raise pyric.error(e.errno, e.strerror)

    # we are only concerned w/ @NL80211_ATTR_STA_INFO
    info = {'rx-bytes': None, 'tx-bytes': None, 'rx-pkts': None, 'tx-pkts': None,
            'tx-bitrate':{}, 'rx-bitrate':{}}

    _, bs, d = nl.nla_find(rmsg, nl80211h.NL80211_ATTR_STA_INFO, False)
    for sidx, sattr in bs: # sidx indexes the enum nl80211_sta_info
        try:
            if sidx == nl80211h.NL80211_STA_INFO_RX_BYTES:
                info['rx-bytes'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_TX_BYTES:
                info['tx-bytes'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_RX_PACKETS:
                info['rx-pkts'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_TX_PACKETS:
                info['tx-pkts'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_TX_RETRIES:
                info['tx-retries'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_TX_FAILED:
                info['tx-failed'] = struct.unpack_from('I', sattr, 0)[0]
            elif sidx == nl80211h.NL80211_STA_INFO_TX_BITRATE:
                info['tx-bitrate'] = _rateinfo_(sattr)
            elif sidx == nl80211h.NL80211_STA_INFO_RX_BITRATE:
                info['rx-bitrate'] = _rateinfo_(sattr)
        except struct.error:
            # ignore this and hope other elements still work
            pass

    return info

################################################################################
#### FILE PRIVATE                                                           ####
################################################################################

IPADDR = re.compile("^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$") # re for ip addr
MACADDR = re.compile("^([0-9a-fA-F]{2}:){5}([0-9a-fA-F]{2})$") # re for mac addr


def _hex2ip4_(v):
    """ :returns: a '.' separated ip4 address from byte stream v """
    if _python3:
        return ".".join([str(c) for c in v])
    else:
        return '.'.join([str(ord(c)) for c in v])

def _hex2mac_(v):
    """
     :param v: packed bytestream of form \xd8\xc7\xc8\x00\x11\x22
     :returns: a ':' separated mac address from byte stream v
    """

    if _python3:
        return ":".join(['{0:02x}'.format(c) for c in v])
    else:
        return ":".join(['{0:02x}'.format(ord(c)) for c in v])


def _mac2hex_(v):
    """
     converts mac address to hex string
     :param v: mac address of form xx:yy:zz:00:11:22
     :returns: mac address as hex string
    """
    try:
        return struct.pack('6B',*[int(x,16) for x in v.split(':')])
    except AttributeError:
        raise pyric.error(pyric.EINVAL, 'Mac address is not valid')
    except struct.error:
        raise pyric.error(pyric.EINVAL, 'Mac address is not 6 octets')

def _validip4_(addr):
    """
     determines validity of ip4 address
     :param addr: ip addr to check
     :returns: True if addr is valid ip, False otherwise
    """
    try:
        if re.match(IPADDR, addr): return True
    except TypeError:
        return False
    return False

def _validmac_(addr):
    """
     determines validity of hw addr
     :param addr: address to check
     :returns: True if addr is valid hw address, False otherwise
    """
    try:
        if re.match(MACADDR, addr): return True
    except TypeError:
        return False
    return False

def _issetf_(flags, flag):
    """
      determines if flag is set
      :param flags: current flag value
      :param flag: flag to check
      :return: True if flag is set
     """
    return (flags & flag) == flag

def _setf_(flags, flag):
    """
     sets flag, adding to flags
     :param flags: current flag value
     :param flag: flag to set
     :return: new flag value
    """
    return flags | flag

def _unsetf_(flags, flag):
    """
     unsets flag, adding to flags
     :param flags: current flag value
     :param flag: flag to unset
     :return: new flag value
    """
    return flags & ~flag

def _flagsget_(dev, *argv):
    """
     gets the device's flags
     :param dev: device name:
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: device flags
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(_flagsget_, dev)

    try:
        flag = sioch.SIOCGIFFLAGS
        ret = io.io_transfer(iosock, flag, ifh.ifreq(dev, flag))
        return struct.unpack_from(ifh.ifr_flags, ret, ifh.IFNAMELEN)[0]
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def _flagsset_(dev, flags, *argv):
    """
     gets the device's flags
     :param dev: device name:
     :param flags: flags to set
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: device flags after operation
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(_flagsset_, dev, flags)

    try:
        flag = sioch.SIOCSIFFLAGS
        ret = io.io_transfer(iosock, flag, ifh.ifreq(dev, flag, [flags]))
        return struct.unpack_from(ifh.ifr_flags, ret, ifh.IFNAMELEN)[0]
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

#### ADDITIONAL PARSING FOR PHYINFO ####

def _iftypes_(i):
    """
     wraps the IFTYPES list to handle index errors
     :param i:
     :returns: the string IFTYPE corresponding to i
    """
    try:
        return IFTYPES[i]
    except IndexError:
        return "Unknown mode ({0})".format(i)

def _bands_(bs):
    """
     extracts supported freqs, rates from bands
     :param bs: a list of one or more unparsed band attributes
     :returns: dict of the form
      band: one of {'2GHz'|'5GHz'|'UNK (n)'} -> band dict
      band dict ->
       HT: HT is supported by the Card on this band
       VHT: VHT is supported by the Card on this band
       rates: list of supported rates
       rfs: list of supported frequencies
       rf-data: list of dicts of rf-data where rf-data[i] contains info
        regarding rf[i]
    """
    # NOTE: in addition to RF and rates there are HT data included in the
    # band info ATT we do not parse these (see "phy info notes 3.txt")
    bands = {}
    for idx, band in bs:
        # the index tell us what band were in (enum nl80211_band)
        try:
            idx = nl80211h.NL80211_BANDS[idx]
        except IndexError:
            idx = "UNK ({0})".format(idx)
        bands[idx] = {'HT': False,
                     'VHT': False,
                     'rates': None,
                     'rfs': None,
                     'rf-data': None}

        # now we delve into multiple levels of nesting
        for bidx,battr in nl.nla_parse_nested(band):
            # There are other data here (see nl80211_h nl80211_band_attr)
            # that we are not currently using
            if bidx == nl80211h.NL80211_BAND_ATTR_RATES:
                try:
                    bands[idx]['rates'] = _band_rates_(battr)
                except nl.error:
                    bands[idx]['rates'] = []
            elif bidx == nl80211h.NL80211_BAND_ATTR_FREQS:
                try:
                    bands[idx]['rfs'], bands[idx]['rf-data'] = _band_rfs_(battr)
                except nl.error:
                    bands[idx]['rfs'], bands[idx]['rf-data'] = [], []
            elif bidx in [nl80211h.NL80211_BAND_ATTR_HT_MCS_SET,
                          nl80211h.NL80211_BAND_ATTR_HT_CAPA,
                          nl80211h.NL80211_BAND_ATTR_HT_AMPDU_FACTOR,
                          nl80211h.NL80211_BAND_ATTR_HT_AMPDU_DENSITY]:
                bands[idx]['HT'] = True
            elif bidx in [nl80211h.NL80211_BAND_ATTR_VHT_MCS_SET,
                          nl80211h.NL80211_BAND_ATTR_VHT_CAPA]:
                bands[idx]['VHT'] = True
    return bands

def _band_rates_(rs):
    """
     unpacks individual rates from packed rates
     :param rs: packed rates
     :returns: a list of rates in Mbits
     NOTE: ATT we ignore any short preamble specifier
    """
    rates = []
    # unlike other nested attributes, the 'index' into rates is actually
    # a counter (which we'll ignore)
    for _, attr in nl.nla_parse_nested(rs):
        # the nested attribute itself is a nested attribute. The idx indexes
        # the enum nl80211_bitrate_attr of which we are only concerned w/ rate
        for idx, bitattr in nl.nla_parse_nested(attr):
            if idx == nl80211h.NL80211_BITRATE_ATTR_RATE:
                rates.append(struct.unpack_from('I', bitattr, 0)[0] * 0.1)
    return rates

def _band_rfs_(rs):
    """
     unpacks individual RFs (and accompanying data) from packed rfs
     :param rs: packed frequencies
     :returns: a tuple t = (freqs: list of supported RFS (MHz), data: list of dicts)
     where for each i in freqs, data[i] is the corresponding data having the
     form {}
    """
    rfs = []
    rfds = []
    # like rates, the index here is a counter and fattr is a nested attribute
    for _, fattr in nl.nla_parse_nested(rs):
        # RF data being compiled ATT we are ignoring DFS related and infrared
        # related. rfd is initially defined with max-tx, radar, 20Mhz and 10Mhz
        # with 'default' values.
        # Additional values may be returned by the kernel. If present they will
        # be appended to not-permitted as the following strings
        #  HT40-, HT40+, 80MHz, 160MHz and outdoor.
        # If present in not-permitted, they represent False Flags
        rfd = {
            'max-tx': 0,        # Card's maximum tx-power on this RF
            'enabled': True,    # w/ current reg. dom. RF is enabled
            '20Mhz': True,      # w/ current reg. dom. 20MHz operation is allowed
            '10Mhz': True,      # w/ current reg. dom. 10MHz operation is allowed
            'radar': False,     # w/ current reg. dom. radar detec. required on RF
            'not-permitted': [] # additional flags
        }
        for rfi, rfattr in nl.nla_parse_nested(fattr):
            # rfi is the index into enum nl80211_frequency_attr
            if rfi == nl80211h.NL80211_FREQUENCY_ATTR_FREQ:
                rfs.append(struct.unpack_from('I', rfattr, 0)[0])
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_DISABLED:
                rfd['enabled'] = False
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_MAX_TX_POWER: # in mBm
                rfd['max-tx'] = struct.unpack_from('I', rfattr, 0)[0] / 100
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_HT40_MINUS:
                rfd['not-permitted'].append('HT40-')
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_HT40_PLUS:
                rfd['not-permitted'].append('HT40+')
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_80MHZ:
                rfd['not-permitted'].append('80MHz')
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_160MHZ:
                rfd['not-permitted'].append('160MHz')
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_INDOOR_ONLY:
                rfd['not-permitted'].append('outdoor')
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_20MHZ:
                rfd['20MHz'] = False
            elif rfi == nl80211h.NL80211_FREQUENCY_ATTR_NO_10MHZ:
                rfd['10MHz'] = False
        rfds.append(rfd)
    return rfs, rfds

def _unparsed_rf_(band):
    """
     (LEGACY) extract list of supported freqs packed byte stream band
     :param band: packed byte string from NL80211_ATTR_WIPHY_BANDS
     :returns: list of supported frequencies
    """
    rfs = []
    for freq in channels.freqs():
        if band.find(struct.pack("I", freq)) != -1:
            rfs.append(freq)
    return rfs

def _commands_(command):
    """
     converts numeric commands to string version
     :param command: list of command constants
     :returns: list of supported commands as strings
    """
    cs = []
    for _,cmd in command: # rather than index, commands use a counter, ignore it
        try:

            #                     <- 2 -><-  4 ->
            # ignore count, use numeric command to lookup string version in form
            # @NL80211_CMD_<CMD> and strip "@NL80211_CMD_". NOTE: some numeric
            # commands may have multiple string synonyms, in that case, take the
            # first one. Finally, make it lowercase
            cmd = cmdbynum(struct.unpack_from('I', cmd, 0)[0])
            if type(cmd) is type([]): cmd = cmd[0]
            cs.append(cmd[13:].lower()) # skip NL80211_CMD_
        except KeyError:
            # kernel 4 added commands not found in kernel 3 nlh8022.h.
            # keep this just in case new commands pop up again
            cs.append("unknown cmd ({0})".format(cmd))
    return cs

def _ciphers_(ciphers):
    """
     identifies supported ciphers
     :param ciphers: the cipher suite stream
     :returns: a list of supported ciphers
    """
    ss = []
    for cipher in ciphers: # ciphers is a set and not nested
        try:
            ss.append(wlan.WLAN_CIPHER_SUITE_SELECTORS[cipher])
        except KeyError as e:
            # we could do nothing, or append 'rsrv' but we'll add a little
            # for testing/future identificaion purposes
            ss.append('RSRV-{0}'.format(hex(int(e.__str__()))))
    return ss

#### ADDITIONAL PARSING FOR STAINFO

def _rateinfo_(ri):
    """
     parses the rate info stream returning a bitrate dict
     :param ri: rate info stream
     :returns: bitrate dict having the key->value pairs
      rate: bitrate in 100kbits/s
      legacy: fallback bitrate in 100kbits/s (only present if rate is not determined)
      mcs-index: mcs index (0..32) (only present if 802.11n)
      gi: guard interval oneof {0=short|1=long} (only present if 802.11n)
      width: channel width oneof {20|40}
     NOTE: references enum nl80211_rate_info
    """
    bitrate = {'rate': None, 'legacy': None, 'mcs-index': None,
               'gi': 1, 'width': 20}
    for i, attr in nl.nla_parse_nested(ri):
        if i == nl80211h.NL80211_RATE_INFO_BITRATE32:
            bitrate['rate'] = struct.unpack_from('I', attr, 0)[0] * 0.1
        elif i == nl80211h.NL80211_RATE_INFO_BITRATE: # legacy fallback rate
            bitrate['legacy'] = struct.unpack_from('H', attr, 0)[0]
        elif i == nl80211h.NL80211_RATE_INFO_MCS:
            bitrate['mcs-index'] = struct.unpack_from('B', attr, 0)[0]
        elif i == nl80211h.NL80211_RATE_INFO_40_MHZ_WIDTH: # flag
            bitrate['width'] = 40
        elif i == nl80211h.NL80211_RATE_INFO_SHORT_GI: # flag
            bitrate['gi'] = 0

    # clean it up before returning
    # remove legacy if we have rate or make rate = legacy if we dont have rate
    # remove mcs-index and short gi and 40 MHz if there is no mcs-index
    if bitrate['legacy'] and not bitrate['rate']: bitrate['rate'] = bitrate['legacy']
    if bitrate['rate'] and bitrate['legacy']: del bitrate['legacy']
    if bitrate['mcs-index'] is None:
        del bitrate['mcs-index']
        del bitrate['gi']
        del bitrate['width']

    return bitrate

#### NETLINK/IOCTL PARAMETERS ####

def _ifindex_(dev, *argv):
    """
     gets the ifindex for device
     :param dev: device name:
     :param argv: ioctl socket at argv[0] (or empty)
     :returns: ifindex of device
     NOTE: the ifindex can aslo be found in /sys/class/net/<nic>/ifindex
    """
    try:
        iosock = argv[0]
    except IndexError:
        return _iostub_(_ifindex_, dev)

    try:
        flag = sioch.SIOCGIFINDEX
        ret = io.io_transfer(iosock, flag, ifh.ifreq(dev, flag))
        return struct.unpack_from(ifh.ifr_ifindex, ret, ifh.IFNAMELEN)[0]
    except AttributeError as e:
        raise pyric.error(pyric.EINVAL, e)
    except struct.error as e:
        raise pyric.error(pyric.EUNDEF, "Error parsing results: {0}".format(e))
    except io.error as e:
        raise pyric.error(e.errno, e.strerror)

def _familyid_(nlsock):
    """
     extended version: get the family id
     :param nlsock: netlink socket
     :returns: the family id of nl80211
     NOTE:
      In addition to the family id, we get:
       CTRL_ATTR_FAMILY_NAME = nl80211\x00
       CTRL_ATTR_VERSION = \x01\x00\x00\x00 = 1
       CTRL_ATTR_HDRSIZE = \x00\x00\x00\x00 = 0
       CTRL_ATTR_MAXATTR = \xbf\x00\x00\x00 = 191
       CTRL_ATTR_OPS
       CTRL_ATTR_MCAST_GROUPS
      but for now, these are not used
    """
    global _FAM80211ID_
    if _FAM80211ID_ is None:
        # family id is not instantiated, do so now
        msg = nl.nlmsg_new(nltype=genlh.GENL_ID_CTRL,
                           cmd=genlh.CTRL_CMD_GETFAMILY,
                           flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
        nl.nla_put_string(msg,
                          nl80211h.NL80211_GENL_NAME,
                          genlh.CTRL_ATTR_FAMILY_NAME)
        nl.nl_sendmsg(nlsock, msg)
        rmsg = nl.nl_recvmsg(nlsock)
        _FAM80211ID_ = nl.nla_find(rmsg, genlh.CTRL_ATTR_FAMILY_ID)
    return _FAM80211ID_

#### TRANSLATION FUNCTIONS ####

def _iostub_(fct, *argv):
    """
     translates from traditional ioctl <cmd> to extended <cmd>ex
     :param fct: function to translate to
     :param argv: parameters to the function
     :returns: the results of fct
    """
    iosock = io.io_socket_alloc()
    try:
        argv = list(argv) + [iosock]
        return fct(*argv)
    except pyric.error:
        raise # catch and rethrow
    finally:
        io.io_socket_free(iosock)

def _nlstub_(fct, *argv):
    """
     translates from traditional netlink <cmd> to extended <cmd>ex
     :param fct: function to translate to
     :param argv: parameters to the function
     :returns: rresults of fucntion
    """
    nlsock = None
    try:
        nlsock = nl.nl_socket_alloc(timeout=2)
        argv = list(argv) + [nlsock]
        return fct(*argv)
    except pyric.error:
        raise
    finally:
        if nlsock: nl.nl_socket_free(nlsock)

#### PENDING ####

def _fut_chset(card, ch, chw, *argv):
    """
     set current channel on device (iw phy <card.phy> set channel <ch> <chw>
     :param card: Card object
     :param ch: channel number
     :param chw: channel width oneof {None|'HT20'|'HT40-'|'HT40+'}
     :param argv: netlink socket at argv[0] (or empty)
     uses the newer NL80211_CMD_SET_CHANNEL vice iw's depecrated version which
     uses *_SET_WIPHY however, ATT does not work raise Errno 22 Invalid Argument
     NOTE: This only works for cards in monitor mode
    """
    if ch not in channels.channels(): raise pyric.error(pyric.EINVAL, "Invalid channel")
    if chw not in channels.CHTYPES: raise pyric.error(pyric.EINVAL, "Invalid channel width")
    try:
        nlsock = argv[0]
    except IndexError:
        return _nlstub_(_fut_chset, card, ch, chw)

    msg = nl.nlmsg_new(nltype=_familyid_(nlsock),
                       cmd=nl80211h.NL80211_CMD_SET_CHANNEL,
                       flags=nlh.NLM_F_REQUEST | nlh.NLM_F_ACK)
    nl.nla_put_u32(msg, card.idx, nl80211h.NL80211_ATTR_IFINDEX)
    nl.nla_put_u32(msg, channels.ch2rf(ch), nl80211h.NL80211_ATTR_WIPHY_FREQ)
    nl.nla_put_u32(msg, channels.CHTYPES.index(chw), nl80211h.NL80211_ATTR_WIPHY_CHANNEL_TYPE)
    nl.nl_sendmsg(nlsock, msg)
    _ = nl.nl_recvmsg(nlsock)