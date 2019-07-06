import tempfile
import logging
from functools import partial
from operator import itemgetter
from typing import Dict, Union, List, Callable, Optional, IO
from types import ModuleType
from contextlib import suppress

import discord
import pyborg.commands
import requests
import toml
import venusian
import attr

from pyborg.util.awoo import normalize_awoos
from pyborg.util import voicesynth


logger = logging.getLogger(__name__)


@attr.s
class PyborgDiscord(discord.Client):
    """This is the pyborg discord client module.
    It connects over http to a running pyborg/http service."""
    toml_file: str = attr.ib()  # any old path
    multi_port: int = attr.ib(default=2001)
    multiplexing: bool = attr.ib(default=True)
    multi_server: str = attr.ib(default="localhost")
    registry = attr.ib(default=attr.Factory(lambda self: Registry(self), takes_self=True))
    garf_map = attr.ib(default={})
    voice_enabled: bool = attr.ib(default=False)
    speak_mode: bool = attr.ib(init=False)

    def __attrs_post_init__(self) -> None:
        self.settings = toml.load(self.toml_file)
        with suppress(KeyError):
            self.multiplexing = self.settings['pyborg']['multiplex']
            self.multi_server = self.settings['pyborg']['multiplex_server']
            self.multi_port = self.settings['pyborg']['multiplex_port']
            self.voice_enabled = self.settings['discord']['voice']
        if not self.multiplexing:
            # self.pyborg = pyborg.pyborg.pyborg()
            # pyb config parsing isn't ready for python 3.
            raise NotImplementedError
        else:
            self.pyborg = None
        super().__init__()

    def our_start(self, opus_lib="libopus.so.0") -> None:
        self.scan()
        if self.voice_enabled:
            discord.opus.load_opus(opus_lib)
        if 'token' in self.settings['discord']:
            self.run(self.settings['discord']['token'])
        else:
            logger.error("No Token. Set one in your conf file.")

    async def on_ready(self) -> None:
        print('Logged in as')
        print(self.user.name)
        print(self.user.id)
        print('------')

    def clean_msg(self, message: discord.Message) -> str:
        return ' '.join(message.content.split())

    def _vc_with_people(self, guild: discord.Guild) -> discord.VoiceChannel:
        "returns the most populated channel"
        table: List[Dict[str, Union[int, discord.VoiceChannel]]] = []
        logging.info("vc channels: %s", guild.voice_channels)
        for chan in guild.voice_channels:
            pop = len(chan.members)
            if guild.voice_client:
                if chan == guild.voice_client.channel:
                    pop - 1
            logging.info("vc_w_ppl chan: %s", chan)
            table.append({"population": pop, "channel": chan})
        logging.info("vc_with_people: %s", table)
        sorted_table = sorted(table, key=itemgetter("population"))
        logging.info("vc_with_people sorted: %s", sorted_table)
        return sorted_table[0]["channel"]

    async def _temp_dl(self, url: str) -> IO:
        "write out url into tmp file and returns it"
        tmp = tempfile.NamedTemporaryFile(delete=False)
        # tmp = open("keep.mp3", "wb")
        ret = requests.get(url, stream=True)
        for chunk in ret.iter_content(1024):
            tmp.write(chunk)
        return tmp

    async def _shove_message_into_voice(self, body: str, guild: discord.Guild, target_vchannel: Optional[discord.VoiceChannel] = None, finalizer: Optional[Callable] = None) -> None:
        logger.info("entered _shove_message_into_voice")
        # get the audio data from voiceforge
        result = await voicesynth.mk_audio(body)
        tmp = await self._temp_dl(result.final_url)
        if target_vchannel:
            voice_channel = target_vchannel
        else:
            voice_channel = self._vc_with_people(guild)
        possible_channel = discord.utils.get(self.voice_clients, channel=voice_channel)
        if possible_channel:
            # we don't need to connect in this case.
            voice_client = possible_channel
        else:
            voice_client = await voice_channel.connect()
        aud = discord.FFmpegPCMAudio(tmp.name)
        voice_client.play(aud, after=finalizer)
        await voicesynth.post_hoc_delete(result.session_id)

    def _extract_emoji(self, msg: str, server_emojis: List[str]) -> str:
        """extract an emoji, returns a str ready to be munged"""
        # s[s.find("<:"):s.find(">")+1] the general idea here
        start = msg.find("<:")
        attempted_emoji = msg[start + 2: msg.find(":", start + 2)]
        logger.info("_extract_emoji:attempting to emoji: %s", attempted_emoji)
        if attempted_emoji in server_emojis:
            # now replace the range from start to end with the extracted emoji
            end = msg.find(">", start) + 1
            before, _, after = msg.partition(msg[start:end])
            logger.debug(_)
            incoming_message = before + attempted_emoji + after
            logger.info(incoming_message)
            return incoming_message
        else:
            logger.info("_extract_emoji:someone did a fucky wucky")
            logger.debug("_extract_emoji:OOPSIE WOOPSIE!! Uwu We made a fucky wucky!! A wittle fucko boingo! The code monkeys at our headquarters are working VEWY HAWD to fix this!")
            return msg

    async def on_message(self, message: discord.Message) -> None:
        """message.content  ~= <@221134985560588289> you should play dota"""
        logger.debug(message.content)
        if message.content and message.content[0] == "!":
            command_name = message.content.split()[0][1:]
            if command_name in ["list", "help"]:
                help_text = "I have a bunch of commands:"
                for k, v in self.registry.registered.items():
                    help_text += " !{}".format(k)
                await message.channel.send(help_text)
            elif command_name in ['leave', 'part']:
                if message.guild.voice_client:
                    self.loop.create_task(message.guild.voice_client.disconnect())
                    return
            elif command_name in ["garf", "garfmode"]:
                logger.info("garfmode enabled: %s", message.guild)
                self.garf_map[message.guild] = True
                try:
                    target = message.author.voice.channel
                except AttributeError:
                    await message.channel.send("behavior undefined currently")
                    target = None
                self.loop.create_task(self._shove_message_into_voice(" ".join(message.content.split()[1:]), message.guild, target))
                return

            else:
                if command_name in self.registry.registered:
                    command = self.registry.registered[command_name]
                    logger.info("Running command %s", command)
                    logger.info("pass message?: %s", command.pass_msg)
                    if command.pass_msg:
                        await message.channel.send(command(msg=message.content))
                    else:
                        await message.channel.send(command())
                return
        if message.author == self.user:
            logger.info("Not learning/responding to self")
            return

        # Custom Emoji handling here
        # DEBUG:pyborg.mod.mod_discord:<:weedminion:392111795642433556>
        # is this cached? is this fast? who the fuck knows
        if "<:" in message.content:
            e = message.guild.emojis
            server_emojis = [x.name for x in e]
            logger.debug("got server emojis as: %s", str(server_emojis))
            incoming_message = self._extract_emoji(message.content, server_emojis)

        else:
            incoming_message = message.content

        # Strip nicknames for pyborg
        line_list = list()
        for x in incoming_message.split():
            if x.startswith("<@!"):
                x = "#nick"
            line_list.append(x)

        line = " ".join(line_list)
        try:
            if self.settings["discord"]["plaintext_ping"]:
                line = line.replace(message.guild.me.display_name, "#nick")
                line = line.replace(message.guild.me.display_name.lower(), "#nick")
        except KeyError:
            pass

        logger.debug("post nick replace: %s", line)
        line = normalize_awoos(line)

        if self.settings['discord']['learning']:
            self.learn(line)

        if self.user.mentioned_in(message) or self._plaintext_name(message):
            async with message.channel.typing():
                msg = self.reply(line)
                logger.debug("on message: %s" % msg)
                if msg:
                    logger.debug("Sending message...")
                    # if custom emoji: replace to <:weedminion:392111795642433556>
                    # message.server map to full custom emoji
                    emoji_map = {x.name: x for x in message.guild.emojis}
                    for word in msg.split():
                        if word in emoji_map:
                            e = emoji_map[word]
                            msg = msg.replace(word, "<:{}:{}>".format(e.name, e.id))
                    msg = msg.replace("#nick", str(message.author.mention))
                    msg = msg.replace("@everyone", "`@everyone`")
                    msg = msg.replace("@here", "`@here`")
                    if self.garf_map[message.guild]:
                        self.loop.create_task(self._shove_message_into_voice(msg, message.guild))
                    await message.channel.send(msg)

    def _plaintext_name(self, message: discord.Message) -> bool:
        "returns true if should ping with plaintext nickname per-server if configured"
        try:
            if self.settings["discord"]["plaintext_ping"]:
                return message.guild.me.display_name.lower() in message.content.lower()
            else:
                return False
        except KeyError:
            return False

    def learn(self, body: str) -> None:
        """thin wrapper for learn to switch to multiplex mode"""
        if self.settings['pyborg']['multiplex']:
            ret = requests.post("http://{}:{}/learn".format(self.multi_server, self.multi_port), data={"body": body})
            if ret.status_code > 499:
                logger.error("Internal Server Error in pyborg_http. see logs.")
            else:
                ret.raise_for_status()

    def reply(self, body: str) -> Union[str, None]:
        """thin wrapper for reply to switch to multiplex mode"""
        if self.settings['pyborg']['multiplex']:
            ret = requests.post("http://{}:{}/reply".format(self.multi_server, self.multi_port), data={"body": body})
            if ret.status_code == requests.codes.ok:
                reply = ret.text
                logger.debug("got reply: %s", reply)
            elif ret.status_code > 499:
                logger.error("Internal Server Error in pyborg_http. see logs.")
                return None
            else:
                ret.raise_for_status()
            return reply
        else:
            raise NotImplementedError

    def teardown(self) -> None:
        pass

    def scan(self, module: ModuleType = pyborg.commands) -> None:
        self.scanner = venusian.Scanner(registry=self.registry)
        self.scanner.scan(module)


class Registry(object):
    """Command registry of decorated pyborg commands"""
    def __init__(self, mod: PyborgDiscord) -> None:
        self.registered: Dict[str, Callable] = {}
        self.mod = mod

    def add(self, name: str, ob: Callable, internals: bool, pass_msg: bool) -> None:
        self.registered[name] = ob
        if internals:
            self.registered[name] = partial(ob, self.mod.multiplexing, multi_server="http://{}:{}/".format(self.mod.multi_server, self.mod.multi_port))
            self.registered[name].pass_msg = False
        if pass_msg:
            self.registered[name].pass_msg = True
        else:
            self.registered[name].pass_msg = False
