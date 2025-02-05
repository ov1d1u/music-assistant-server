"""
MusicAssistant Players Controller.

Handles all logic to control supported players,
which are provided by Player Providers.

"""

from __future__ import annotations

import asyncio
import functools
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, TypeVar, cast

from music_assistant_models.constants import (
    PLAYER_CONTROL_FAKE,
    PLAYER_CONTROL_NATIVE,
    PLAYER_CONTROL_NONE,
)
from music_assistant_models.enums import (
    EventType,
    MediaType,
    PlayerFeature,
    PlayerState,
    PlayerType,
    ProviderType,
)
from music_assistant_models.errors import (
    AlreadyRegisteredError,
    PlayerCommandFailed,
    PlayerUnavailableError,
    UnsupportedFeaturedException,
)
from music_assistant_models.media_items import UniqueList
from music_assistant_models.player import Player, PlayerMedia
from music_assistant_models.player_control import PlayerControl  # noqa: TC002

from music_assistant.constants import (
    CONF_AUTO_PLAY,
    CONF_ENTRY_ANNOUNCE_VOLUME,
    CONF_ENTRY_ANNOUNCE_VOLUME_MAX,
    CONF_ENTRY_ANNOUNCE_VOLUME_MIN,
    CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY,
    CONF_ENTRY_PLAYER_ICON,
    CONF_HIDE_PLAYER,
    CONF_MUTE_CONTROL,
    CONF_PLAYERS,
    CONF_POWER_CONTROL,
    CONF_TTS_PRE_ANNOUNCE,
    CONF_VOLUME_CONTROL,
)
from music_assistant.helpers.api import api_command
from music_assistant.helpers.tags import async_parse_tags
from music_assistant.helpers.throttle_retry import Throttler
from music_assistant.helpers.util import TaskManager, get_changed_values
from music_assistant.models.core_controller import CoreController
from music_assistant.models.player_provider import PlayerProvider

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine, Iterator

    from music_assistant_models.config_entries import CoreConfig, PlayerConfig
    from music_assistant_models.player_queue import PlayerQueue


_PlayerControllerT = TypeVar("_PlayerControllerT", bound="PlayerController")
_R = TypeVar("_R")
_P = ParamSpec("_P")


def handle_player_command(
    func: Callable[Concatenate[_PlayerControllerT, _P], Awaitable[_R]],
) -> Callable[Concatenate[_PlayerControllerT, _P], Coroutine[Any, Any, _R | None]]:
    """Check and log commands to players."""

    @functools.wraps(func)
    async def wrapper(self: _PlayerControllerT, *args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        """Log and handle_player_command commands to players."""
        player_id = kwargs["player_id"] if "player_id" in kwargs else args[0]
        if (player := self._players.get(player_id)) is None or not player.available:
            # player not existent
            self.logger.warning(
                "Ignoring command %s for unavailable player %s",
                func.__name__,
                player_id,
            )
            return

        self.logger.debug(
            "Handling command %s for player %s",
            func.__name__,
            player.display_name,
        )
        try:
            await func(self, *args, **kwargs)
        except Exception as err:
            raise PlayerCommandFailed(str(err)) from err

    return wrapper


class PlayerController(CoreController):
    """Controller holding all logic to control registered players."""

    domain: str = "players"

    def __init__(self, *args, **kwargs) -> None:
        """Initialize core controller."""
        super().__init__(*args, **kwargs)
        self._players: dict[str, Player] = {}
        self._controls: dict[str, PlayerControl] = {}
        self._prev_states: dict[str, dict] = {}
        self.manifest.name = "Players controller"
        self.manifest.description = (
            "Music Assistant's core controller which manages all players from all providers."
        )
        self.manifest.icon = "speaker-multiple"
        self._poll_task: asyncio.Task | None = None
        self._player_throttlers: dict[str, Throttler] = {}
        self._player_locks: dict[str, asyncio.Lock] = {}
        # TEMP 2024-11-20: register some aliases for renamed commands
        # remove after a few releases
        self.mass.register_api_command("players/cmd/sync", self.cmd_group)
        self.mass.register_api_command("players/cmd/unsync", self.cmd_ungroup)
        self.mass.register_api_command("players/cmd/sync_many", self.cmd_group_many)
        self.mass.register_api_command("players/cmd/unsync_many", self.cmd_ungroup_many)

    async def setup(self, config: CoreConfig) -> None:
        """Async initialize of module."""
        self._poll_task = self.mass.create_task(self._poll_players())

    async def close(self) -> None:
        """Cleanup on exit."""
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()

    @property
    def providers(self) -> list[PlayerProvider]:
        """Return all loaded/running MusicProviders."""
        return self.mass.get_providers(ProviderType.PLAYER)  # type: ignore=return-value

    def __iter__(self) -> Iterator[Player]:
        """Iterate over (available) players."""
        return iter(self._players.values())

    @api_command("players/all")
    def all(
        self,
        return_unavailable: bool = True,
        return_disabled: bool = False,
    ) -> list[Player]:
        """Return all registered players."""
        return [
            player
            for player in self._players.values()
            if (player.available or return_unavailable) and (player.enabled or return_disabled)
        ]

    @api_command("players/player_controls")
    def player_controls(
        self,
    ) -> list[PlayerControl]:
        """Return all registered playercontrols."""
        return list(self._controls.values())

    @api_command("players/get")
    def get(
        self,
        player_id: str,
        raise_unavailable: bool = False,
    ) -> Player | None:
        """Return Player by player_id."""
        if player := self._players.get(player_id):
            if (not player.available or not player.enabled) and raise_unavailable:
                msg = f"Player {player_id} is not available"
                raise PlayerUnavailableError(msg)
            return player
        if raise_unavailable:
            msg = f"Player {player_id} is not available"
            raise PlayerUnavailableError(msg)
        return None

    @api_command("players/get_by_name")
    def get_by_name(self, name: str) -> Player | None:
        """Return Player by name or None if no match is found."""
        return next((x for x in self._players.values() if x.name == name), None)

    # Player commands

    @api_command("players/cmd/stop")
    @handle_player_command
    async def cmd_stop(self, player_id: str) -> None:
        """Send STOP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active
        if active_queue := self.mass.player_queues.get(player.active_source):
            await self.mass.player_queues.stop(active_queue.queue_id)
            return
        # send to player provider
        async with self._player_throttlers[player.player_id]:
            if player_provider := self.get_player_provider(player.player_id):
                await player_provider.cmd_stop(player.player_id)

    @api_command("players/cmd/play")
    @handle_player_command
    async def cmd_play(self, player_id: str) -> None:
        """Send PLAY (unpause) command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.state == PlayerState.PLAYING:
            self.logger.info(
                "Ignore PLAY request to player %s: player is already playing", player.display_name
            )
            return
        # Redirect to queue controller if it is active
        active_source = player.active_source or player.player_id
        if (active_queue := self.mass.player_queues.get(active_source)) and active_queue.items:
            await self.mass.player_queues.play(active_queue.queue_id)
            return
        # send to player provider
        player_provider = self.get_player_provider(player.player_id)
        async with self._player_throttlers[player.player_id]:
            await player_provider.cmd_play(player.player_id)

    @api_command("players/cmd/pause")
    @handle_player_command
    async def cmd_pause(self, player_id: str) -> None:
        """Send PAUSE command to given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if PlayerFeature.PAUSE not in player.supported_features:
            # if player does not support pause, we need to send stop
            self.logger.info(
                "Player %s does not support pause, using STOP instead",
                player.display_name,
            )
            await self.cmd_stop(player.player_id)
            return
        player_provider = self.get_player_provider(player.player_id)
        await player_provider.cmd_pause(player.player_id)

        async def _watch_pause(_player_id: str) -> None:
            player = self.get(_player_id, True)
            count = 0
            # wait for pause
            while count < 5 and player.state == PlayerState.PLAYING:
                count += 1
                await asyncio.sleep(1)
            # wait for unpause
            if player.state != PlayerState.PAUSED:
                return
            count = 0
            while count < 30 and player.state == PlayerState.PAUSED:
                count += 1
                await asyncio.sleep(1)
            # if player is still paused when the limit is reached, send stop
            if player.state == PlayerState.PAUSED:
                await self.cmd_stop(_player_id)

        # we auto stop a player from paused when its paused for 30 seconds
        if not player.announcement_in_progress:
            self.mass.create_task(_watch_pause(player_id))

    @api_command("players/cmd/play_pause")
    async def cmd_play_pause(self, player_id: str) -> None:
        """Toggle play/pause on given player.

        - player_id: player_id of the player to handle the command.
        """
        player = self._get_player_with_redirect(player_id)
        if player.state == PlayerState.PLAYING:
            await self.cmd_pause(player.player_id)
        else:
            await self.cmd_play(player.player_id)

    @api_command("players/cmd/seek")
    async def cmd_seek(self, player_id: str, position: int) -> None:
        """Handle SEEK command for given player.

        - player_id: player_id of the player to handle the command.
        - position: position in seconds to seek to in the current playing item.
        """
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active
        active_source = player.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            await self.mass.player_queues.seek(active_queue.queue_id, position)
            return
        if PlayerFeature.SEEK not in player.supported_features:
            msg = f"Player {player.display_name} does not support seeking"
            raise UnsupportedFeaturedException(msg)
        player_prov = self.get_player_provider(player.player_id)
        await player_prov.cmd_seek(player.player_id, position)

    @api_command("players/cmd/next")
    async def cmd_next_track(self, player_id: str) -> None:
        """Handle NEXT TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active
        active_source = player.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            await self.mass.player_queues.next(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS not in player.supported_features:
            msg = f"Player {player.display_name} does not support skipping to the next track."
            raise UnsupportedFeaturedException(msg)
        player_prov = self.get_player_provider(player.player_id)
        await player_prov.cmd_next(player.player_id)

    @api_command("players/cmd/previous")
    async def cmd_previous_track(self, player_id: str) -> None:
        """Handle PREVIOUS TRACK command for given player."""
        player = self._get_player_with_redirect(player_id)
        # Redirect to queue controller if it is active
        active_source = player.active_source or player.player_id
        if active_queue := self.mass.player_queues.get(active_source):
            await self.mass.player_queues.previous(active_queue.queue_id)
            return
        if PlayerFeature.NEXT_PREVIOUS not in player.supported_features:
            msg = f"Player {player.display_name} does not support skipping to the previous track."
            raise UnsupportedFeaturedException(msg)
        player_prov = self.get_player_provider(player.player_id)
        await player_prov.cmd_previous(player.player_id)

    @api_command("players/cmd/power")
    @handle_player_command
    async def cmd_power(self, player_id: str, powered: bool, skip_update: bool = False) -> None:
        """Send POWER command to given player.

        - player_id: player_id of the player to handle the command.
        - powered: bool if player should be powered on or off.
        """
        player = self.get(player_id, True)

        if player.powered == powered:
            return  # nothing to do

        # ungroup player at power off
        player_was_synced = player.synced_to is not None
        if not powered:
            # this will handle both synced players and group players
            # NOTE: ungroup will be ignored if the player is not grouped or synced
            await self.cmd_ungroup(player_id)

        # always stop player at power off
        if (
            not powered
            and not player_was_synced
            and player.state in (PlayerState.PLAYING, PlayerState.PAUSED)
        ):
            await self.cmd_stop(player_id)

        # power off all synced childs when player is a sync leader
        elif not powered and player.type == PlayerType.PLAYER and player.group_childs:
            async with TaskManager(self.mass) as tg:
                for member in self.iter_group_members(player, True):
                    if member.power_control == PLAYER_CONTROL_NONE:
                        continue
                    tg.create_task(self.cmd_power(member.player_id, False))

        # handle actual power command
        if player.power_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support power control"
            )
        if player.power_control == PLAYER_CONTROL_NATIVE:
            # player supports power command natively: forward to player provider
            player_provider = self.get_player_provider(player_id)
            async with self._player_throttlers[player_id]:
                await player_provider.cmd_power(player_id, powered)
        elif player.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake power control - so we only (optimistically) update the state
            # short sleep: allow the stop command to process and prevent race conditions
            await asyncio.sleep(0.2)
        else:
            # handle external player control
            player_control = self._controls.get(player.power_control)
            control_name = player_control.name if player_control else player.power_control
            self.logger.debug("Redirecting power command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_power:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            if powered:
                await player_control.power_on()
            else:
                await player_control.power_off()

        # always optimistically set the power state to update the UI
        # as fast as possible and prevent race conditions
        player.powered = powered
        # reset active source on power off
        if not powered:
            player.active_source = None

        if not skip_update:
            self.update(player_id)

        # handle 'auto play on power on' feature
        if (
            not player.active_group
            and powered
            and self.mass.config.get_raw_player_config_value(player_id, CONF_AUTO_PLAY, False)
            and player.active_source in (None, player_id)
        ):
            await self.mass.player_queues.resume(player_id)

    @api_command("players/cmd/volume_set")
    @handle_player_command
    async def cmd_volume_set(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given player.

        - player_id: player_id of the player to handle the command.
        - volume_level: volume level (0..100) to set on the player.
        """
        # TODO: Implement PlayerControl
        player = self.get(player_id, True)
        if player.type == PlayerType.GROUP:
            # redirect to group volume control
            await self.cmd_group_volume(player_id, volume_level)
            return

        if player.volume_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support volume control"
            )
        if player.volume_control == PLAYER_CONTROL_NATIVE:
            # player supports volume command natively: forward to player provider
            player_provider = self.get_player_provider(player_id)
            async with self._player_throttlers[player_id]:
                await player_provider.cmd_volume_set(player_id, volume_level)
        else:
            # handle external player control
            player_control = self._controls.get(player.volume_control)
            control_name = player_control.name if player_control else player.volume_control
            self.logger.debug("Redirecting volume command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_volume:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            async with self._player_throttlers[player_id]:
                await player_control.volume_set(volume_level)

    @api_command("players/cmd/volume_up")
    @handle_player_command
    async def cmd_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get(player_id)):
            return
        if player.volume_level < 5 or player.volume_level > 95:
            step_size = 1
        elif player.volume_level < 20 or player.volume_level > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = min(100, self._players[player_id].volume_level + step_size)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/volume_down")
    @handle_player_command
    async def cmd_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given player.

        - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get(player_id)):
            return
        if player.volume_level < 5 or player.volume_level > 95:
            step_size = 1
        elif player.volume_level < 20 or player.volume_level > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, self._players[player_id].volume_level - step_size)
        await self.cmd_volume_set(player_id, new_volume)

    @api_command("players/cmd/group_volume")
    @handle_player_command
    async def cmd_group_volume(self, player_id: str, volume_level: int) -> None:
        """Send VOLUME_SET command to given playergroup.

        Will send the new (average) volume level to group child's.
            - player_id: player_id of the playergroup to handle the command.
            - volume_level: volume level (0..100) to set on the player.
        """
        group_player = self.get(player_id, True)
        assert group_player
        # handle group volume by only applying the volume to powered members
        cur_volume = group_player.group_volume
        new_volume = volume_level
        volume_dif = new_volume - cur_volume
        coros = []
        for child_player in self.iter_group_members(
            group_player, only_powered=True, exclude_self=False
        ):
            if child_player.volume_control == PLAYER_CONTROL_NONE:
                continue
            cur_child_volume = child_player.volume_level
            new_child_volume = int(cur_child_volume + volume_dif)
            new_child_volume = max(0, new_child_volume)
            new_child_volume = min(100, new_child_volume)
            coros.append(self.cmd_volume_set(child_player.player_id, new_child_volume))
        await asyncio.gather(*coros)

    @api_command("players/cmd/group_volume_up")
    @handle_player_command
    async def cmd_group_volume_up(self, player_id: str) -> None:
        """Send VOLUME_UP command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        group_player = self.get(player_id, True)
        assert group_player
        cur_volume = group_player.group_volume
        if cur_volume < 5 or cur_volume > 95:
            step_size = 1
        elif cur_volume < 20 or cur_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = min(100, cur_volume + step_size)
        await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/group_volume_down")
    @handle_player_command
    async def cmd_group_volume_down(self, player_id: str) -> None:
        """Send VOLUME_DOWN command to given playergroup.

        - player_id: player_id of the player to handle the command.
        """
        group_player = self.get(player_id, True)
        assert group_player
        cur_volume = group_player.group_volume
        if cur_volume < 5 or cur_volume > 95:
            step_size = 1
        elif cur_volume < 20 or cur_volume > 80:
            step_size = 2
        else:
            step_size = 5
        new_volume = max(0, cur_volume - step_size)
        await self.cmd_group_volume(player_id, new_volume)

    @api_command("players/cmd/volume_mute")
    @handle_player_command
    async def cmd_volume_mute(self, player_id: str, muted: bool) -> None:
        """Send VOLUME_MUTE command to given player.

        - player_id: player_id of the player to handle the command.
        - muted: bool if player should be muted.
        """
        player = self.get(player_id, True)
        assert player
        if player.mute_control == PLAYER_CONTROL_NONE:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support muting"
            )
        if player.mute_control == PLAYER_CONTROL_NATIVE:
            # player supports mute command natively: forward to player provider
            player_provider = self.get_player_provider(player_id)
            async with self._player_throttlers[player_id]:
                await player_provider.cmd_volume_mute(player_id, muted)
        elif player.power_control == PLAYER_CONTROL_FAKE:
            # user wants to use fake mute control - so we use volume instead
            self.logger.debug(
                "Using volume for muting for player %s",
                player.display_name,
            )
            if muted:
                player.previous_volume_level = player.volume_level
                player.volume_muted = True
                await self.cmd_volume_set(player_id, 0)
            else:
                player.volume_muted = False
                await self.cmd_volume_set(player_id, player.previous_volume_level)
        else:
            # handle external player control
            player_control = self._controls.get(player.mute_control)
            control_name = player_control.name if player_control else player.mute_control
            self.logger.debug("Redirecting mute command to PlayerControl %s", control_name)
            if not player_control or not player_control.supports_mute:
                raise UnsupportedFeaturedException(
                    f"Player control {control_name} is not available"
                )
            async with self._player_throttlers[player_id]:
                await player_control.mute_set(muted)

    @api_command("players/cmd/play_announcement")
    async def play_announcement(
        self,
        player_id: str,
        url: str,
        use_pre_announce: bool | None = None,
        volume_level: int | None = None,
    ) -> None:
        """Handle playback of an announcement (url) on given player."""
        player = self.get(player_id, True)
        if not url.startswith("http"):
            raise PlayerCommandFailed("Only URLs are supported for announcements")
        # prevent multiple announcements at the same time to the same player with a lock
        if player_id not in self._player_locks:
            self._player_locks[player_id] = lock = asyncio.Lock()
        else:
            lock = self._player_locks[player_id]
        async with lock:
            try:
                # mark announcement_in_progress on player
                player.announcement_in_progress = True
                # determine if the player has native announcements support
                native_announce_support = (
                    PlayerFeature.PLAY_ANNOUNCEMENT in player.supported_features
                )
                # determine pre-announce from (group)player config
                if use_pre_announce is None and "tts" in url:
                    use_pre_announce = await self.mass.config.get_player_config_value(
                        player_id,
                        CONF_TTS_PRE_ANNOUNCE,
                    )
                # if player type is group with all members supporting announcements,
                # we forward the request to each individual player
                if player.type == PlayerType.GROUP and (
                    all(
                        PlayerFeature.PLAY_ANNOUNCEMENT in x.supported_features
                        for x in self.iter_group_members(player)
                    )
                ):
                    # forward the request to each individual player
                    async with TaskManager(self.mass) as tg:
                        for group_member in player.group_childs:
                            tg.create_task(
                                self.play_announcement(
                                    group_member,
                                    url=url,
                                    use_pre_announce=use_pre_announce,
                                    volume_level=volume_level,
                                )
                            )
                    return
                self.logger.info(
                    "Playback announcement to player %s (with pre-announce: %s): %s",
                    player.display_name,
                    use_pre_announce,
                    url,
                )
                # create a PlayerMedia object for the announcement so
                # we can send a regular play-media call downstream
                announcement = PlayerMedia(
                    uri=self.mass.streams.get_announcement_url(player_id, url, use_pre_announce),
                    media_type=MediaType.ANNOUNCEMENT,
                    title="Announcement",
                    custom_data={"url": url, "use_pre_announce": use_pre_announce},
                )
                # handle native announce support
                if native_announce_support:
                    if prov := self.mass.get_provider(player.provider):
                        announcement_volume = self.get_announcement_volume(player_id, volume_level)
                        await prov.play_announcement(player_id, announcement, announcement_volume)
                        return
                # use fallback/default implementation
                await self._play_announcement(player, announcement, volume_level)
            finally:
                player.announcement_in_progress = False

    @handle_player_command
    async def play_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle PLAY MEDIA on given player.

        - player_id: player_id of the player to handle the command.
        - media: The Media that needs to be played on the player.
        """
        player = self._get_player_with_redirect(player_id)
        # power on the player if needed
        if player.powered is False and player.power_control != PLAYER_CONTROL_NONE:
            await self.cmd_power(player.player_id, True)
        player_prov = self.get_player_provider(player.player_id)
        await player_prov.play_media(
            player_id=player.player_id,
            media=media,
        )

    @api_command("players/cmd/select_source")
    async def select_source(self, player_id: str, source: str) -> None:
        """
        Handle SELECT SOURCE command on given player.

        - player_id: player_id of the player to handle the command.
        - source: The ID of the source that needs to be activated/selected.
        """
        player = self.get(player_id, True)
        # basic check if player supports source selection
        if PlayerFeature.SELECT_SOURCE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support source selection"
            )
        # basic check if source is valid for player
        if not any(x for x in player.source_list if x.id == source):
            raise PlayerCommandFailed(
                f"{source} is an invalid source for player {player.display_name}"
            )
        # forward to player provider
        provider = self.mass.get_provider(player.provider)
        await provider.select_source(player_id, source)

    async def enqueue_next_media(self, player_id: str, media: PlayerMedia) -> None:
        """Handle enqueuing of a next media item on the player."""
        player = self.get(player_id, raise_unavailable=True)
        if PlayerFeature.ENQUEUE not in player.supported_features:
            raise UnsupportedFeaturedException(
                f"Player {player.display_name} does not support enqueueing"
            )
        player_prov = self.mass.get_provider(player.provider)
        async with self._player_throttlers[player_id]:
            await player_prov.enqueue_next_media(player_id=player_id, media=media)

    @api_command("players/cmd/group")
    @handle_player_command
    async def cmd_group(self, player_id: str, target_player: str) -> None:
        """Handle GROUP command for given player.

        Join/add the given player(id) to the given (leader) player/sync group.
        If the target player itself is already synced to another player, this may fail.
        If the player can not be synced with the given target player, this may fail.

            - player_id: player_id of the player to handle the command.
            - target_player: player_id of the syncgroup leader or group player.
        """
        await self.cmd_group_many(target_player, [player_id])

    @api_command("players/cmd/group_many")
    async def cmd_group_many(self, target_player: str, child_player_ids: list[str]) -> None:
        """Join given player(s) to target player."""
        parent_player: Player = self.get(target_player, True)
        prev_group_childs = parent_player.group_childs.copy()
        if PlayerFeature.SET_MEMBERS not in parent_player.supported_features:
            msg = f"Player {parent_player.name} does not support group commands"
            raise UnsupportedFeaturedException(msg)

        if parent_player.synced_to:
            # guard edge case: player already synced to another player
            raise PlayerCommandFailed(
                f"Player {parent_player.name} is already synced to another player on its own, "
                "you need to ungroup it first before you can join other players to it.",
            )

        # filter all player ids on compatibility and availability
        final_player_ids: UniqueList[str] = UniqueList()
        for child_player_id in child_player_ids:
            if child_player_id == target_player:
                continue
            if not (child_player := self.get(child_player_id)) or not child_player.available:
                self.logger.warning("Player %s is not available", child_player_id)
                continue
            # check if player can be synced/grouped with the target player
            if not (
                child_player_id in parent_player.can_group_with
                or child_player.provider in parent_player.can_group_with
            ):
                raise UnsupportedFeaturedException(
                    f"Player {child_player.name} can not be grouped with {parent_player.name}"
                )

            if child_player.synced_to and child_player.synced_to == target_player:
                continue  # already synced to this target

            if child_player.group_childs and child_player.state != PlayerState.IDLE:
                # guard edge case: childplayer is already a sync leader on its own
                raise PlayerCommandFailed(
                    f"Player {child_player.name} is already synced with other players, "
                    "you need to ungroup it first before you can join it to another player.",
                )
            if child_player.synced_to:
                # player already synced to another player, ungroup first
                self.logger.warning(
                    "Player %s is already synced to another player, ungrouping first",
                    child_player.name,
                )
                await self.cmd_ungroup(child_player.player_id)
            # power on the player if needed
            if not child_player.powered and child_player.power_control != PLAYER_CONTROL_NONE:
                await self.cmd_power(child_player.player_id, True, skip_update=True)
            # if we reach here, all checks passed
            final_player_ids.append(child_player_id)
            # set active source if player is synced
            child_player.active_source = parent_player.player_id

        # forward command to the player provider after all (base) sanity checks
        player_provider = self.get_player_provider(target_player)
        async with self._player_throttlers[target_player]:
            try:
                await player_provider.cmd_group_many(target_player, final_player_ids)
            except Exception:
                # restore sync state if the command failed
                parent_player.group_childs.set(prev_group_childs)
                raise

    @api_command("players/cmd/ungroup")
    @handle_player_command
    async def cmd_ungroup(self, player_id: str) -> None:
        """Handle UNGROUP command for given player.

        Remove the given player from any (sync)groups it currently is synced to.
        If the player is not currently grouped to any other player,
        this will silently be ignored.

            - player_id: player_id of the player to handle the command.
        """
        if not (player := self.get(player_id)):
            self.logger.warning("Player %s is not available", player_id)
            return

        if (
            player.active_group
            and (group_player := self.get(player.active_group))
            and (
                PlayerFeature.SET_MEMBERS in group_player.supported_features
                or group_player.provider.startswith("player_group")
            )
        ):
            # the player is part of a (permanent) groupplayer and the user tries to ungroup
            # redirect the command to the group provider
            group_provider = self.mass.get_provider(group_player.provider)
            await group_provider.cmd_ungroup_member(player_id, group_player.player_id)
            return

        if not (player.synced_to or player.group_childs):
            return  # nothing to do

        if PlayerFeature.SET_MEMBERS not in player.supported_features:
            self.logger.warning("Player %s does not support (un)group commands", player.name)
            return

        # handle (edge)case where un ungroup command is sent to a sync leader;
        # we dissolve the entire syncgroup in this case.
        # while maybe not strictly needed to do this for all player providers,
        # we do this to keep the functionality consistent across all providers
        if player.group_childs:
            self.logger.warning(
                "Detected ungroup command to player %s which is a sync(group) leader, "
                "all sync members will be ungrouped!",
                player.name,
            )
            async with TaskManager(self.mass) as tg:
                for group_child_id in player.group_childs:
                    if group_child_id == player_id:
                        continue
                    tg.create_task(self.cmd_ungroup(group_child_id))
            return

        # (optimistically) reset active source player if it is ungrouped
        player.active_source = None

        # forward command to the player provider
        if player_provider := self.get_player_provider(player_id):
            await player_provider.cmd_ungroup(player_id)
        # if the command succeeded we optimistically reset the sync state
        # this is to prevent race conditions and to update the UI as fast as possible
        player.synced_to = None

    @api_command("players/cmd/ungroup_many")
    async def cmd_ungroup_many(self, player_ids: list[str]) -> None:
        """Handle UNGROUP command for all the given players."""
        for player_id in list(player_ids):
            await self.cmd_ungroup(player_id)

    def set(self, player: Player) -> None:
        """Set/Update player details on the controller."""
        if player.player_id not in self._players:
            # new player
            self.register(player)
            return
        self._players[player.player_id] = player
        self.update(player.player_id)

    async def register(self, player: Player) -> None:
        """Register a new player on the controller."""
        if self.mass.closing:
            return
        player_id = player.player_id

        if player_id in self._players:
            msg = f"Player {player_id} is already registered"
            raise AlreadyRegisteredError(msg)

        # make sure that the player's provider is set to the lookup key (=instance id)
        prov = self.mass.get_provider(player.provider)
        if not prov or prov.lookup_key != player.provider:
            raise RuntimeError(f"Invalid provider ID given: {player.provider}")

        # make sure a default config exists
        self.mass.config.create_default_player_config(
            player_id, player.provider, player.name, player.enabled_by_default
        )

        player.enabled = self.mass.config.get(f"{CONF_PLAYERS}/{player_id}/enabled", True)

        # register playerqueue for this player
        self.mass.create_task(self.mass.player_queues.on_player_register(player))

        # register throttler for this player
        self._player_throttlers[player_id] = Throttler(1, 0.2)

        self._players[player_id] = player

        # ignore disabled players
        if not player.enabled:
            return

        # ensure initial player state gets populated with values from config
        player_config = await self.mass.config.get_player_config(player_id)
        self._set_player_state_from_config(player, player_config)

        self.logger.info(
            "Player registered: %s/%s",
            player_id,
            player.display_name,
        )

        self.mass.signal_event(EventType.PLAYER_ADDED, object_id=player.player_id, data=player)
        # always call update to fix special attributes like display name, group volume etc.
        self.update(player.player_id)

    async def register_or_update(self, player: Player) -> None:
        """Register a new player on the controller or update existing one."""
        if self.mass.closing:
            return

        if player.player_id in self._players:
            self._players[player.player_id] = player
            self.update(player.player_id)
            return

        await self.register(player)

    def remove(self, player_id: str, cleanup_config: bool = True) -> None:
        """Remove a player from the player manager."""
        player = self._players.pop(player_id, None)
        if player is None:
            return
        self.logger.info("Player removed: %s", player.name)
        self.mass.player_queues.on_player_remove(player_id)
        if cleanup_config:
            self.mass.config.remove(f"players/{player_id}")
        self._prev_states.pop(player_id, None)
        self.mass.signal_event(EventType.PLAYER_REMOVED, player_id)

    def update(  # noqa: PLR0915
        self, player_id: str, skip_forward: bool = False, force_update: bool = False
    ) -> None:
        """Update player state."""
        if self.mass.closing:
            return
        if player_id not in self._players:
            return
        player = self._players[player_id]
        prev_state = self._prev_states.get(player_id, {})
        player.active_source = self._get_active_source(player)
        # prefer any overridden name from config
        player.display_name = (
            self.mass.config.get_raw_player_config_value(player.player_id, "name")
            or player.name
            # use prev value (e.g. some fallback)
            or player.display_name
        )
        # handle player controls
        if player.power_control == PLAYER_CONTROL_NONE:
            player.powered = None
        elif player.power_control == PLAYER_CONTROL_FAKE:
            player.powered = True if player.powered is None else player.powered
        elif player.power_control != PLAYER_CONTROL_NATIVE:
            if player_control := self._controls.get(player.power_control):
                player.powered = player_control.power_state
        if player.volume_control == PLAYER_CONTROL_NONE:
            player.volume_level = None
        elif player.volume_control != PLAYER_CONTROL_NATIVE:
            if player_control := self._controls.get(player.volume_control):
                player.volume_level = player_control.volume_level
        if player.mute_control == PLAYER_CONTROL_NONE:
            player.volume_muted = None
        elif player.mute_control not in (PLAYER_CONTROL_NATIVE, PLAYER_CONTROL_FAKE):
            if player_control := self._controls.get(player.mute_control):
                player.volume_muted = player_control.volume_muted
        # correct group_members if needed
        if player.group_childs == [player.player_id]:
            player.group_childs.clear()
        elif (
            player.group_childs
            and player.player_id not in player.group_childs
            and player.type == PlayerType.PLAYER
        ):
            player.group_childs.set([player.player_id, *player.group_childs])
        if player.active_group and player.active_group == player.player_id:
            player.active_group = None
        # Auto correct player state if player is synced (or group child)
        # This is because some players/providers do not accurately update this info
        # for the sync child's.
        if player.synced_to and (sync_leader := self.get(player.synced_to)):
            player.state = sync_leader.state
            player.elapsed_time = sync_leader.elapsed_time
            player.elapsed_time_last_updated = sync_leader.elapsed_time_last_updated
        # calculate group volume
        player.group_volume = self._get_group_volume_level(player)
        if player.type == PlayerType.GROUP:
            player.volume_level = player.group_volume

        # correct available state if needed
        if not player.enabled:
            player.available = False

        # basic throttle: do not send state changed events if player did not actually change
        new_state = self._players[player_id].to_dict()
        changed_values = get_changed_values(
            prev_state,
            new_state,
            ignore_keys=[
                "elapsed_time_last_updated",
                "seq_no",
                "last_poll",
            ],
        )
        self._prev_states[player_id] = new_state

        if not player.enabled and not force_update:
            # ignore updates for disabled players
            return

        # always signal update to the playerqueue (regardless of changes)
        self.mass.player_queues.on_player_update(player, changed_values)

        if len(changed_values) == 0 and not force_update:
            # nothing changed
            return

        if changed_values.keys() == {"elapsed_time"} and not force_update:
            # ignore elapsed_time only changes
            return

        # handle DSP reload of the leader when on grouping and ungrouping
        prev_child_count = len(prev_state.get("group_childs", []))
        new_child_count = len(new_state.get("group_childs", []))
        is_player_group = player.provider.startswith("player_group")

        # handle special case for PlayerGroups: since there are no leaders,
        # DSP still always work with a single player in the group.
        multi_device_dsp_threshold = 1 if is_player_group else 0

        prev_is_multiple_devices = prev_child_count > multi_device_dsp_threshold
        new_is_multiple_devices = new_child_count > multi_device_dsp_threshold

        if prev_is_multiple_devices != new_is_multiple_devices:
            supports_multi_device_dsp = PlayerFeature.MULTI_DEVICE_DSP in player.supported_features
            dsp_enabled: bool
            if is_player_group:
                # Since player groups do not have leaders, we will use the only child
                # that was in the group before and after the change
                if prev_is_multiple_devices:
                    if childs := new_state.get("group_childs"):
                        # We shrank the group from multiple players to a single player
                        # So the now only child will control the DSP
                        dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
                    else:
                        dsp_enabled = False
                elif childs := prev_state.get("group_childs"):
                    # We grew the group from a single player to multiple players,
                    # let's see if the previous single player had DSP enabled
                    dsp_enabled = self.mass.config.get_player_dsp_config(childs[0]).enabled
                else:
                    dsp_enabled = False
            else:
                dsp_enabled = self.mass.config.get_player_dsp_config(player_id).enabled
            if dsp_enabled and not supports_multi_device_dsp:
                # We now know that that the group configuration has changed so:
                # - multi-device DSP is not supported
                # - we switched from a group with multiple players to a single player
                #   (or vice versa)
                # - the leader has DSP enabled
                self.mass.create_task(self.mass.players.on_player_dsp_change(player_id))

        # signal player update on the eventbus
        self.mass.signal_event(EventType.PLAYER_UPDATED, object_id=player_id, data=player)

        # handle player becoming unavailable
        if "available" in changed_values and not player.available:
            self._handle_player_unavailable(player)

        if skip_forward and not force_update:
            return

        # update/signal group player(s) child's when group updates
        for child_player in self.iter_group_members(player, exclude_self=True):
            self.update(child_player.player_id, skip_forward=True)
        # update/signal group player(s) when child updates
        for group_player in self._get_player_groups(player, powered_only=False):
            if player_prov := self.mass.get_provider(group_player.provider):
                self.mass.create_task(player_prov.poll_player(group_player.player_id))

    async def register_player_control(self, player_control: PlayerControl) -> None:
        """Register a new PlayerControl on the controller."""
        if self.mass.closing:
            return
        control_id = player_control.id

        if control_id in self._controls:
            msg = f"PlayerControl {control_id} is already registered"
            raise AlreadyRegisteredError(msg)

        # make sure that the playercontrol's provider is set to the lookup_key
        prov = self.mass.get_provider(player_control.provider)
        if not prov or prov.lookup_key != player_control.provider:
            raise RuntimeError(f"Invalid provider ID given: {player_control.provider}")

        self._controls[control_id] = player_control

        self.logger.info(
            "PlayerControl registered: %s/%s",
            control_id,
            player_control.name,
        )

        # always call update to update any attached players etc.
        self.update_player_control(player_control.id)

    async def register_or_update_player_control(self, player_control: PlayerControl) -> None:
        """Register a new playercontrol on the controller or update existing one."""
        if self.mass.closing:
            return
        if player_control.id in self._controls:
            self._controls[player_control.id] = player_control
            self.update_player_control(player_control.id)
            return
        await self.register_player_control(player_control)

    def update_player_control(self, control_id: str) -> None:
        """Update playercontrol state."""
        if self.mass.closing:
            return
        # update all players that are using this control
        for player in self._players.values():
            if control_id in (player.power_control, player.volume_control, player.mute_control):
                self.update(player.player_id)

    def remove_player_control(self, control_id: str) -> None:
        """Remove a player_control from the player manager."""
        control = self._controls.pop(control_id, None)
        if control is None:
            return
        self._controls.pop(control_id, None)
        self.logger.info("PlayerControl removed: %s", control.name)

    def get_player_provider(self, player_id: str) -> PlayerProvider:
        """Return PlayerProvider for given player."""
        player = self._players[player_id]
        player_provider = self.mass.get_provider(player.provider)
        return cast(PlayerProvider, player_provider)

    def get_announcement_volume(self, player_id: str, volume_override: int | None) -> int | None:
        """Get the (player specific) volume for a announcement."""
        volume_strategy = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.key,
            CONF_ENTRY_ANNOUNCE_VOLUME_STRATEGY.default_value,
        )
        volume_strategy_volume = self.mass.config.get_raw_player_config_value(
            player_id,
            CONF_ENTRY_ANNOUNCE_VOLUME.key,
            CONF_ENTRY_ANNOUNCE_VOLUME.default_value,
        )
        volume_level = volume_override
        if volume_level is None and volume_strategy == "absolute":
            volume_level = volume_strategy_volume
        elif volume_level is None and volume_strategy == "relative":
            player = self.get(player_id)
            volume_level = player.volume_level + volume_strategy_volume
        elif volume_level is None and volume_strategy == "percentual":
            player = self.get(player_id)
            percentual = (player.volume_level / 100) * volume_strategy_volume
            volume_level = player.volume_level + percentual
        if volume_level is not None:
            announce_volume_min = self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN.key,
                CONF_ENTRY_ANNOUNCE_VOLUME_MIN.default_value,
            )
            volume_level = max(announce_volume_min, volume_level)
            announce_volume_max = self.mass.config.get_raw_player_config_value(
                player_id,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX.key,
                CONF_ENTRY_ANNOUNCE_VOLUME_MAX.default_value,
            )
            volume_level = min(announce_volume_max, volume_level)
        # ensure the result is an integer
        return None if volume_level is None else int(volume_level)

    def iter_group_members(
        self,
        group_player: Player,
        only_powered: bool = False,
        only_playing: bool = False,
        active_only: bool = False,
        exclude_self: bool = True,
    ) -> Iterator[Player]:
        """Get (child) players attached to a group player or syncgroup."""
        for child_id in list(group_player.group_childs):
            if child_player := self.get(child_id, False):
                if not child_player.available or not child_player.enabled:
                    continue
                if only_powered and child_player.powered is False:
                    continue
                if active_only and child_player.active_group != group_player.player_id:
                    continue
                if exclude_self and child_player.player_id == group_player.player_id:
                    continue
                if only_playing and child_player.state not in (
                    PlayerState.PLAYING,
                    PlayerState.PAUSED,
                ):
                    continue
                yield child_player

    async def wait_for_state(
        self,
        player: Player,
        wanted_state: PlayerState,
        timeout: float = 60.0,
        minimal_time: float = 0,
    ) -> None:
        """Wait for the given player to reach the given state."""
        start_timestamp = time.time()
        self.logger.debug(
            "Waiting for player %s to reach state %s", player.display_name, wanted_state
        )
        try:
            async with asyncio.timeout(timeout):
                while player.state != wanted_state:
                    await asyncio.sleep(0.1)

        except TimeoutError:
            self.logger.debug(
                "Player %s did not reach state %s within the timeout of %s seconds",
                player.display_name,
                wanted_state,
                timeout,
            )
        elapsed_time = round(time.time() - start_timestamp, 2)
        if elapsed_time < minimal_time:
            self.logger.debug(
                "Player %s reached state %s too soon (%s vs %s seconds) - add fallback sleep...",
                player.display_name,
                wanted_state,
                elapsed_time,
                minimal_time,
            )
            await asyncio.sleep(minimal_time - elapsed_time)
        else:
            self.logger.debug(
                "Player %s reached state %s within %s seconds",
                player.display_name,
                wanted_state,
                elapsed_time,
            )

    async def on_player_config_change(self, config: PlayerConfig, changed_keys: set[str]) -> None:
        """Call (by config manager) when the configuration of a player changes."""
        player_disabled = "enabled" in changed_keys and not config.enabled
        if not (player := self.get(config.player_id)):
            return
        # ensure player state gets updated with any updated config
        self._set_player_state_from_config(player, config)
        resume_queue: PlayerQueue | None = self.mass.player_queues.get(player.active_source)
        # signal player provider that the config changed
        if player_provider := self.mass.get_provider(config.provider):
            with suppress(PlayerUnavailableError):
                await player_provider.on_player_config_change(config, changed_keys)
        if player_disabled:
            # edge case: ensure that the player is powered off if the player gets disabled
            if player.power_control != PLAYER_CONTROL_NONE:
                await self.cmd_power(config.player_id, False)
            elif player.state != PlayerState.IDLE:
                await self.cmd_stop(config.player_id)
            player.available = False
        # if the PlayerQueue was playing, restart playback
        # TODO: add property to ConfigEntry if it requires a restart of playback on change
        elif not player_disabled and resume_queue and resume_queue.state == PlayerState.PLAYING:
            self.mass.call_later(1, self.mass.player_queues.resume, resume_queue.queue_id)
        # check for group memberships that need to be updated
        if player_disabled and player.active_group and player_provider:
            # try to remove from the group
            group_player = self.get(player.active_group)
            with suppress(UnsupportedFeaturedException, PlayerCommandFailed):
                await player_provider.set_members(
                    player.active_group,
                    [x for x in group_player.group_childs if x != player.player_id],
                )
        player.enabled = config.enabled

    async def on_player_dsp_change(self, player_id: str) -> None:
        """Call (by config manager) when the DSP settings of a player change."""
        # signal player provider that the config changed
        if not (player := self.get(player_id)):
            return
        if player.state == PlayerState.PLAYING:
            self.logger.info("Restarting playback of Player %s after DSP change", player_id)
            # this will restart ffmpeg with the new settings
            self.mass.call_later(0, self.mass.player_queues.resume, player.active_source)

    def _get_player_with_redirect(self, player_id: str) -> Player:
        """Get player with check if playback related command should be redirected."""
        player = self.get(player_id, True)
        if player.synced_to and (sync_leader := self.get(player.synced_to)):
            self.logger.info(
                "Player %s is synced to %s and can not accept "
                "playback related commands itself, "
                "redirected the command to the sync leader.",
                player.name,
                sync_leader.name,
            )
            return sync_leader
        if player.active_group and (active_group := self.get(player.active_group)):
            self.logger.info(
                "Player %s is part of a playergroup and can not accept "
                "playback related commands itself, "
                "redirected the command to the group leader.",
                player.name,
            )
            return active_group
        return player

    def _get_player_groups(
        self, player: Player, available_only: bool = True, powered_only: bool = False
    ) -> Iterator[Player]:
        """Return all groupplayers the given player belongs to."""
        for _player in self:
            if _player.player_id == player.player_id:
                continue
            if _player.type != PlayerType.GROUP:
                continue
            if available_only and not _player.available:
                continue
            if powered_only and _player.powered is False:
                continue
            if player.player_id in _player.group_childs:
                yield _player

    def _get_active_source(self, player: Player) -> str:
        """Return the active_source id for given player."""
        # if player is synced, return group leader's active source
        if player.synced_to and (parent_player := self.get(player.synced_to)):
            return parent_player.active_source
        # if player has group active, return those details
        if player.active_group and (group_player := self.get(player.active_group)):
            return self._get_active_source(group_player)
        # defaults to the player's own player id if no active source set
        return player.active_source or player.player_id

    def _get_group_volume_level(self, player: Player) -> int:
        """Calculate a group volume from the grouped members."""
        if len(player.group_childs) == 0:
            # player is not a group or syncgroup
            return player.volume_level or 0
        # calculate group volume from all (turned on) players
        group_volume = 0
        active_players = 0
        for child_player in self.iter_group_members(player, only_powered=True, exclude_self=False):
            if child_player.volume_control == PLAYER_CONTROL_NONE:
                continue
            if child_player.volume_level is None:
                continue
            group_volume += child_player.volume_level
            active_players += 1
        if active_players:
            group_volume = group_volume / active_players
        return int(group_volume)

    def _handle_player_unavailable(self, player: Player) -> None:
        """Handle a player becoming unavailable."""
        if player.synced_to:
            self.mass.create_task(self.cmd_ungroup(player.player_id))
            # also set this optimistically because the above command will most likely fail
            player.synced_to = None
            return
        for group_child_id in player.group_childs:
            if group_child_id == player.player_id:
                continue
            if child_player := self.get(group_child_id):
                self.mass.create_task(self.cmd_power(group_child_id, False, True))
                # also set this optimistically because the above command will most likely fail
                child_player.synced_to = None
            player.group_childs.clear()
        if player.active_group and (group_player := self.get(player.active_group)):
            # remove player from group if its part of a group
            group_player = self.get(player.active_group)
            if player.player_id in group_player.group_childs:
                group_player.group_childs.remove(player.player_id)

    def _set_player_state_from_config(self, player: Player, config: PlayerConfig) -> None:
        """Set player state from config."""
        player.display_name = config.name or player.name or config.default_name or player.player_id
        player.hidden = config.get_value(CONF_HIDE_PLAYER)
        player.icon = config.get_value(CONF_ENTRY_PLAYER_ICON.key)
        player.power_control = config.get_value(CONF_POWER_CONTROL)
        player.volume_control = config.get_value(CONF_VOLUME_CONTROL)
        player.mute_control = config.get_value(CONF_MUTE_CONTROL)

    async def _play_announcement(
        self,
        player: Player,
        announcement: PlayerMedia,
        volume_level: int | None = None,
    ) -> None:
        """Handle (default/fallback) implementation of the play announcement feature.

        This default implementation will;
        - stop playback of the current media (if needed)
        - power on the player (if needed)
        - raise the volume a bit
        - play the announcement (from given url)
        - wait for the player to finish playing
        - restore the previous power and volume
        - restore playback (if needed and if possible)

        This default implementation will only be used if the player
        (provider) has no native support for the PLAY_ANNOUNCEMENT feature.
        """
        prev_power = player.powered
        prev_state = player.state
        prev_synced_to = player.synced_to
        queue = self.mass.player_queues.get(player.active_source)
        prev_queue_active = queue and queue.active
        prev_item_id = player.current_item_id
        # ungroup player if its currently synced
        if prev_synced_to:
            self.logger.debug(
                "Announcement to player %s - ungrouping player...",
                player.display_name,
            )
            await self.cmd_ungroup(player.player_id)
        # stop player if its currently playing
        elif prev_state in (PlayerState.PLAYING, PlayerState.PAUSED):
            self.logger.debug(
                "Announcement to player %s - stop existing content (%s)...",
                player.display_name,
                prev_item_id,
            )
            await self.cmd_stop(player.player_id)
            # wait for the player to stop
            await self.wait_for_state(player, PlayerState.IDLE, 10, 0.4)
        # adjust volume if needed
        # in case of a (sync) group, we need to do this for all child players
        prev_volumes: dict[str, int] = {}
        async with TaskManager(self.mass) as tg:
            for volume_player_id in player.group_childs or (player.player_id,):
                if not (volume_player := self.get(volume_player_id)):
                    continue
                # catch any players that have a different source active
                if (
                    volume_player.active_source
                    not in (
                        player.active_source,
                        volume_player.player_id,
                        None,
                    )
                    and volume_player.state == PlayerState.PLAYING
                ):
                    self.logger.warning(
                        "Detected announcement to playergroup %s while group member %s is playing "
                        "other content, this may lead to unexpected behavior.",
                        player.display_name,
                        volume_player.display_name,
                    )
                    tg.create_task(self.cmd_stop(volume_player.player_id))
                prev_volume = volume_player.volume_level
                announcement_volume = self.get_announcement_volume(volume_player_id, volume_level)
                temp_volume = announcement_volume or player.volume_level
                if temp_volume != prev_volume:
                    prev_volumes[volume_player_id] = prev_volume
                    self.logger.debug(
                        "Announcement to player %s - setting temporary volume (%s)...",
                        volume_player.display_name,
                        announcement_volume,
                    )
                    tg.create_task(
                        self.cmd_volume_set(volume_player.player_id, announcement_volume)
                    )
        # play the announcement
        self.logger.debug(
            "Announcement to player %s - playing the announcement on the player...",
            player.display_name,
        )
        await self.play_media(player_id=player.player_id, media=announcement)
        # wait for the player(s) to play
        await self.wait_for_state(player, PlayerState.PLAYING, 10, minimal_time=0.1)
        # wait for the player to stop playing
        if not announcement.duration:
            media_info = await async_parse_tags(announcement.custom_data["url"])
            announcement.duration = media_info.duration or 60
        media_info.duration += 2
        await self.wait_for_state(
            player,
            PlayerState.IDLE,
            max(announcement.duration * 2, 60),
            announcement.duration + 2,
        )
        self.logger.debug(
            "Announcement to player %s - restore previous state...", player.display_name
        )
        # restore volume
        async with TaskManager(self.mass) as tg:
            for volume_player_id, prev_volume in prev_volumes.items():
                tg.create_task(self.cmd_volume_set(volume_player_id, prev_volume))

        await asyncio.sleep(0.2)
        player.current_item_id = prev_item_id
        # either power off the player or resume playing
        if not prev_power and player.power_control != PLAYER_CONTROL_NONE:
            await self.cmd_power(player.player_id, False)
            return
        elif prev_synced_to:
            await self.cmd_group(player.player_id, prev_synced_to)
        elif prev_queue_active and prev_state == PlayerState.PLAYING:
            await self.mass.player_queues.resume(queue.queue_id, True)
        elif prev_state == PlayerState.PLAYING:
            # player was playing something else - try to resume that here
            self.logger.warning("Can not resume %s on %s", prev_item_id, player.display_name)
            # TODO !!

    async def _poll_players(self) -> None:
        """Background task that polls players for updates."""
        while True:
            for player in list(self._players.values()):
                player_id = player.player_id
                # if the player is playing, update elapsed time every tick
                # to ensure the queue has accurate details
                player_playing = player.state == PlayerState.PLAYING
                if player_playing:
                    self.mass.loop.call_soon(self.update, player_id)
                # Poll player;
                if not player.needs_poll:
                    continue
                if (self.mass.loop.time() - player.last_poll) < player.poll_interval:
                    continue
                player.last_poll = self.mass.loop.time()
                if player_prov := self.get_player_provider(player_id):
                    try:
                        await player_prov.poll_player(player_id)
                    except PlayerUnavailableError:
                        player.available = False
                        player.state = PlayerState.IDLE
                        player.powered = None
                    except Exception as err:
                        self.logger.warning(
                            "Error while requesting latest state from player %s: %s",
                            player.display_name,
                            str(err),
                            exc_info=err if self.logger.isEnabledFor(10) else None,
                        )
                    finally:
                        # always update player state
                        self.mass.loop.call_soon(self.update, player_id)
            await asyncio.sleep(1)
