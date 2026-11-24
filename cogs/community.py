"""Suggestions, giveaways, native polls and starboards; no synthetic events."""
import datetime
import io
import json
import time

import discord
from discord import app_commands

from services.community import (CommunityService, CommunityError, SuggestionModal,
                                SuggestionVotes, GiveawayEntry, parse_duration, reply)


def install(bot, connection, get_setting, put_setting, get_level=None):
    service = CommunityService(bot, connection, get_setting, put_setting, get_level)
    bot.community_service = service
    suggestions = app_commands.Group(name='suggestion', description='Suggestions, voting and staff review')
    giveaways = app_commands.Group(name='giveaway', description='Persistent community giveaways')
    polls = app_commands.Group(name='poll', description='Native Discord polls')
    starboard = app_commands.Group(name='starboard', description='Community starred messages')
    for group in (suggestions, giveaways, polls, starboard):
        bot.tree.add_command(group)
    bot.add_listener(service.ready, 'on_ready')
    bot.add_listener(service.star_event, 'on_raw_reaction_add')
    bot.add_listener(service.star_event, 'on_raw_reaction_remove')
    bot.add_listener(service.deleted_message, 'on_raw_message_delete')
    bot.add_listener(service.bulk_deleted_messages, 'on_raw_bulk_message_delete')
    bot.add_listener(service.edited_message, 'on_raw_message_edit')

    async def error(interaction, exc):
        original = getattr(exc, 'original', exc)
        if isinstance(original, CommunityError):
            return await reply(interaction, str(original))
        if isinstance(original, discord.HTTPException):
            return await reply(interaction, 'Discord rejected that operation. Check the bot’s selected-channel permissions.')
        raise exc
    for group in (suggestions, giveaways, polls, starboard):
        group.on_error = error

    def require_guild(interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise CommunityError('Use this command in a server.')

    def require_admin(interaction):
        require_guild(interaction)
        if not service.admin(interaction.user):
            raise CommunityError('Manage Server permission is required.')

    def require_staff(interaction):
        require_guild(interaction)
        if not service.moderator(interaction.user):
            raise CommunityError('Manage Messages or Manage Server permission is required.')

    @bot.tree.command(name='suggest', description='Submit a suggestion with persistent community voting')
    async def suggest(interaction: discord.Interaction, text: str = ''):
        try:
            require_guild(interaction)
            if text:
                await service.submit(interaction, text)
            else:
                if not service.enabled(interaction.guild_id, 'suggestions'):
                    raise CommunityError('Suggestions are disabled in this server.')
                await interaction.response.send_modal(SuggestionModal(service))
        except CommunityError as exc:
            await reply(interaction, str(exc))
        except discord.HTTPException:
            await reply(interaction, 'Discord could not publish the suggestion. Check the selected channel and bot permissions.')

    @suggestions.command(name='channel', description='Select an existing suggestions channel and enable suggestions')
    @app_commands.default_permissions(manage_guild=True)
    async def suggestion_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        require_admin(interaction)
        put_setting(interaction.guild_id, 'suggestions:channel', str(channel.id))
        put_setting(interaction.guild_id, 'module:suggestions', '1')
        await reply(interaction, f'Suggestions enabled in {channel.mention}.')

    @suggestions.command(name='submit', description='Open the suggestion submission form')
    async def suggestion_submit(interaction: discord.Interaction):
        require_guild(interaction)
        if not service.enabled(interaction.guild_id, 'suggestions'):
            raise CommunityError('Suggestions are disabled in this server.')
        await interaction.response.send_modal(SuggestionModal(service))

    @suggestions.command(name='status', description='Approve, deny or reopen a suggestion with a staff response')
    @app_commands.choices(status=[app_commands.Choice(name=x.title(), value=x) for x in ('approved', 'denied', 'pending')])
    async def suggestion_status(interaction: discord.Interaction, suggestion_id: int, status: app_commands.Choice[str], reason: str = ''):
        require_staff(interaction)
        service.suggestion(interaction.guild_id, suggestion_id)
        connection.execute('UPDATE community_suggestions SET status=?,reason=?,reviewer=? WHERE guild=? AND id=?',
            (status.value, reason[:1000], interaction.user.id, interaction.guild_id, suggestion_id))
        connection.commit()
        await interaction.response.defer(ephemeral=True)
        await service.refresh_suggestion(interaction.guild, service.suggestion(interaction.guild_id, suggestion_id))
        await reply(interaction, f'Suggestion status saved: {status.value}.')

    @suggestions.command(name='enabled', description='Enable or disable suggestions and voting for this server')
    @app_commands.default_permissions(manage_guild=True)
    async def suggestion_enabled(interaction: discord.Interaction, enabled: bool):
        require_admin(interaction)
        put_setting(interaction.guild_id, 'module:suggestions', '1' if enabled else '0')
        await reply(interaction, 'Suggestions ' + ('enabled.' if enabled else 'disabled.'))

    @suggestions.command(name='list', description='List recent suggestions from this server')
    async def suggestion_list(interaction: discord.Interaction):
        require_guild(interaction)
        rows = service.query('SELECT * FROM community_suggestions WHERE guild=? ORDER BY id DESC LIMIT 20', (interaction.guild_id,))
        visible = [r for r in rows if (interaction.guild.get_channel(r['channel']) and
                    interaction.guild.get_channel(r['channel']).permissions_for(interaction.user).view_channel)]
        await reply(interaction, '\n'.join(f'#{r["id"]} · {r["status"]} · {r["content"][:70]}' for r in visible) or 'No suggestions visible to you.')

    @suggestions.command(name='info', description='Read a suggestion and its current voting results')
    async def suggestion_info(interaction: discord.Interaction, suggestion: int):
        require_guild(interaction)
        record = service.suggestion(interaction.guild_id, suggestion)
        channel = interaction.guild.get_channel(record['channel'])
        if not channel or not channel.permissions_for(interaction.user).view_channel:
            raise CommunityError('You cannot access that suggestion’s channel.')
        await interaction.response.send_message(embed=service.suggestion_embed(record), ephemeral=True)

    def review_command(name, status):
        async def callback(interaction: discord.Interaction, suggestion: int, reason: str):
            require_staff(interaction)
            record = service.suggestion(interaction.guild_id, suggestion)
            connection.execute('UPDATE community_suggestions SET status=?,reason=?,reviewer=? WHERE guild=? AND id=?',
                (status, reason[:1000], interaction.user.id, interaction.guild_id, suggestion))
            connection.commit()
            record = service.suggestion(interaction.guild_id, suggestion)
            await interaction.response.defer(ephemeral=True)
            await service.refresh_suggestion(interaction.guild, record)
            await reply(interaction, f'Suggestion marked {status}; public staff response updated.')
        suggestions.add_command(app_commands.Command(name=name, description=f'{name.title()} a suggestion with a public reason', callback=callback))
    review_command('approve', 'approved')
    review_command('deny', 'denied')

    @suggestions.command(name='edit', description='Edit your pending suggestion and reset votes for the changed proposal')
    async def suggestion_edit(interaction: discord.Interaction, suggestion: int, text: str):
        require_guild(interaction)
        record = service.suggestion(interaction.guild_id, suggestion)
        if record['author'] != interaction.user.id or record['status'] != 'pending':
            raise CommunityError('Only the author of a pending suggestion can edit it.')
        if not 10 <= len(text.strip()) <= 3500:
            raise CommunityError('Use between 10 and 3500 characters.')
        connection.execute('UPDATE community_suggestions SET content=? WHERE guild=? AND id=?', (text.strip(), interaction.guild_id, suggestion))
        connection.execute('DELETE FROM community_votes WHERE guild=? AND suggestion=?', (interaction.guild_id, suggestion))
        connection.commit()
        await interaction.response.defer(ephemeral=True)
        await service.refresh_suggestion(interaction.guild, service.suggestion(interaction.guild_id, suggestion))
        await reply(interaction, 'Suggestion updated. Previous votes were cleared because the proposal changed.')

    @suggestions.command(name='withdraw', description='Withdraw your pending suggestion and close voting')
    async def suggestion_withdraw(interaction: discord.Interaction, suggestion: int):
        require_guild(interaction)
        record = service.suggestion(interaction.guild_id, suggestion)
        if record['author'] != interaction.user.id or record['status'] != 'pending':
            raise CommunityError('Only the author of a pending suggestion can withdraw it.')
        connection.execute("UPDATE community_suggestions SET status='withdrawn' WHERE guild=? AND id=?", (interaction.guild_id, suggestion))
        connection.commit()
        await interaction.response.defer(ephemeral=True)
        await service.refresh_suggestion(interaction.guild, service.suggestion(interaction.guild_id, suggestion))
        await reply(interaction, 'Suggestion withdrawn; voting closed.')

    @suggestions.command(name='export', description='Export this server’s suggestion records privately')
    @app_commands.default_permissions(manage_guild=True)
    async def suggestion_export(interaction: discord.Interaction):
        require_admin(interaction)
        rows = service.query('SELECT * FROM community_suggestions WHERE guild=? ORDER BY id LIMIT 2000', (interaction.guild_id,))
        payload = json.dumps(rows, indent=2, ensure_ascii=False).encode('utf-8')
        if len(payload) > min(interaction.guild.filesize_limit - 1000, 8_000_000):
            raise CommunityError('Suggestion export exceeds the safe upload limit.')
        await reply(interaction, 'Private suggestion export attached.', file=discord.File(io.BytesIO(payload), filename='suggestions.json'))

    @giveaways.command(name='create', description='Create a real giveaway with a persistent closing deadline')
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_start(interaction: discord.Interaction, prize: str, duration: str, winners: app_commands.Range[int, 1, 20] = 1,
                             channel: discord.TextChannel = None, required_role: discord.Role = None, minimum_level: app_commands.Range[int, 0, 150] = 0):
        require_admin(interaction)
        seconds = parse_duration(duration)
        channel = channel or service.channel(interaction.guild, 'giveaways') or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            raise CommunityError('Choose an existing text channel for the giveaway.')
        if not 1 <= len(prize.strip()) <= 200:
            raise CommunityError('Use a prize title from one to 200 characters.')
        if required_role and (required_role.is_default() or required_role.managed):
            raise CommunityError('Choose a regular member role other than @everyone or a managed role.')
        if connection.execute("SELECT COUNT(*) FROM community_giveaways WHERE guild=? AND status='active'", (interaction.guild_id,)).fetchone()[0] >= 20:
            raise CommunityError('This server already has 20 active giveaways.')
        await interaction.response.defer(ephemeral=True)
        cursor = connection.execute('INSERT INTO community_giveaways(guild,channel,host,prize,ends,count,role,minlevel,created) VALUES(?,?,?,?,?,?,?,?,?)',
            (interaction.guild_id, channel.id, interaction.user.id, prize.strip(), time.time() + seconds, winners,
             required_role.id if required_role else 0, minimum_level, time.time()))
        giveaway_id = cursor.lastrowid
        connection.commit()
        record = service.giveaway(interaction.guild_id, giveaway_id)
        try:
            message = await channel.send(embed=service.giveaway_embed(record), view=GiveawayEntry(service),
                                         allowed_mentions=discord.AllowedMentions.none(), silent=True)
        except BaseException:
            connection.execute('DELETE FROM community_giveaways WHERE guild=? AND id=?', (interaction.guild_id, giveaway_id))
            connection.commit()
            raise
        connection.execute('UPDATE community_giveaways SET message=? WHERE guild=? AND id=?', (message.id, interaction.guild_id, giveaway_id))
        connection.commit()
        put_setting(interaction.guild_id, 'module:giveaways', '1')
        await reply(interaction, f'Giveaway started: {message.jump_url}')

    def conclude(name, reroll):
        async def callback(interaction: discord.Interaction, giveaway: int):
            require_admin(interaction)
            if reroll and service.giveaway(interaction.guild_id, giveaway)['status'] != 'ended':
                raise CommunityError('End the giveaway before rerolling it.')
            await interaction.response.defer(ephemeral=True)
            selected = await service.finish(interaction.guild, giveaway, reroll)
            await reply(interaction, 'Giveaway result published. Selected: ' + (', '.join(str(u) for u in selected) or 'No eligible entries.'))
        giveaways.add_command(app_commands.Command(name=name, description='Reroll completed winners from remaining eligible entries' if reroll else 'End a giveaway and draw eligible winners now', callback=callback))
    conclude('end', False)
    conclude('reroll', True)

    @giveaways.command(name='cancel', description='Cancel a giveaway without selecting winners')
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_cancel(interaction: discord.Interaction, giveaway: int):
        require_admin(interaction)
        async with service.locks[('giveaway', giveaway)]:
            record = service.giveaway(interaction.guild_id, giveaway)
            if record['status'] != 'active':
                raise CommunityError('Only an active giveaway can be cancelled.')
            connection.execute("UPDATE community_giveaways SET status='cancelled' WHERE guild=? AND id=?", (interaction.guild_id, giveaway))
            connection.commit()
            channel = interaction.guild.get_channel(record['channel'])
            if not channel:
                raise CommunityError('Giveaway cancelled in storage; its original channel no longer exists.')
            message = await channel.fetch_message(record['message'])
            await message.edit(embed=service.giveaway_embed(service.giveaway(interaction.guild_id, giveaway)), view=None)
        await reply(interaction, 'Giveaway cancelled without selecting winners.')

    @giveaways.command(name='list', description='List this server’s recent giveaways')
    async def giveaway_list(interaction: discord.Interaction):
        require_guild(interaction)
        rows = service.query('SELECT * FROM community_giveaways WHERE guild=? ORDER BY id DESC LIMIT 20', (interaction.guild_id,))
        visible = [r for r in rows if interaction.guild.get_channel(r['channel']) and interaction.guild.get_channel(r['channel']).permissions_for(interaction.user).view_channel]
        await reply(interaction, '\n'.join(f'#{r["id"]} · {r["status"]} · {r["prize"][:80]}' for r in visible) or 'No giveaways visible to you.')

    @giveaways.command(name='info', description='Read a giveaway’s prize, entry count, requirements and result')
    async def giveaway_info(interaction: discord.Interaction, giveaway: int):
        require_guild(interaction)
        record = service.giveaway(interaction.guild_id, giveaway)
        channel = interaction.guild.get_channel(record['channel'])
        if not channel or not channel.permissions_for(interaction.user).view_channel:
            raise CommunityError('You cannot access that giveaway’s channel.')
        await interaction.response.send_message(embed=service.giveaway_embed(record), ephemeral=True)

    @giveaways.command(name='entries', description='Privately export the entrant list for a giveaway')
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_entries(interaction: discord.Interaction, giveaway: int):
        require_admin(interaction)
        service.giveaway(interaction.guild_id, giveaway)
        rows = service.query('SELECT user,created FROM community_entries WHERE guild=? AND giveaway=? ORDER BY created LIMIT 10000', (interaction.guild_id, giveaway))
        text = 'user_id,entered_unix\n' + '\n'.join(f'{r["user"]},{int(r["created"])}' for r in rows)
        await reply(interaction, f'{len(rows)} saved entrants.', file=discord.File(io.BytesIO(text.encode()), filename=f'giveaway-{giveaway}-entries.csv'))

    @giveaways.command(name='extend', description='Add time to an active giveaway’s stored closing deadline')
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_extend(interaction: discord.Interaction, giveaway: int, duration: str):
        require_admin(interaction)
        async with service.locks[('giveaway', giveaway)]:
            record = service.giveaway(interaction.guild_id, giveaway)
            if record['status'] != 'active' or record['ends'] <= time.time():
                raise CommunityError('Only an active giveaway can be extended.')
            ends = record['ends'] + parse_duration(duration)
            if ends - time.time() > 90 * 86400:
                raise CommunityError('The deadline cannot be more than 90 days from now.')
            connection.execute('UPDATE community_giveaways SET ends=? WHERE guild=? AND id=?', (ends, interaction.guild_id, giveaway))
            connection.commit()
            channel = interaction.guild.get_channel(record['channel'])
            if not channel:
                raise CommunityError('Deadline saved, but the original giveaway channel no longer exists.')
            message = await channel.fetch_message(record['message'])
            await message.edit(embed=service.giveaway_embed(service.giveaway(interaction.guild_id, giveaway)), view=GiveawayEntry(service))
        await reply(interaction, f'Giveaway deadline extended to <t:{int(ends)}:F>.')

    @giveaways.command(name='prize', description='Correct the prize title on an active giveaway')
    @app_commands.default_permissions(manage_guild=True)
    async def giveaway_prize(interaction: discord.Interaction, giveaway: int, prize: str):
        require_admin(interaction)
        record = service.giveaway(interaction.guild_id, giveaway)
        if record['status'] != 'active' or not 1 <= len(prize.strip()) <= 200:
            raise CommunityError('Choose an active giveaway and a prize title from one to 200 characters.')
        connection.execute('UPDATE community_giveaways SET prize=? WHERE guild=? AND id=?', (prize.strip(), interaction.guild_id, giveaway))
        connection.commit()
        channel = interaction.guild.get_channel(record['channel'])
        if not channel:
            raise CommunityError('Prize saved, but the original channel no longer exists.')
        message = await channel.fetch_message(record['message'])
        await message.edit(embed=service.giveaway_embed(service.giveaway(interaction.guild_id, giveaway)), view=GiveawayEntry(service))
        await reply(interaction, 'Giveaway prize title updated.')

    @polls.command(name='create', description='Create a real native Discord poll')
    @app_commands.default_permissions(manage_messages=True)
    async def poll_create(interaction: discord.Interaction, question: str, answers: str, hours: app_commands.Range[int, 1, 168] = 24,
                          multiple: bool = False, channel: discord.TextChannel = None):
        require_staff(interaction)
        options = [x.strip() for x in answers.split('|') if x.strip()]
        if not 2 <= len(options) <= 10 or any(len(x) > 55 for x in options) or not 1 <= len(question) <= 300:
            raise CommunityError('Use a question up to 300 characters and 2–10 answers up to 55 characters, separated with |.')
        channel = channel or interaction.channel
        if not isinstance(channel, discord.TextChannel):
            raise CommunityError('Select an existing text channel.')
        if not channel.permissions_for(interaction.user).send_messages:
            raise CommunityError('You cannot send messages in the selected channel.')
        poll = discord.Poll(question, duration=datetime.timedelta(hours=hours), multiple=multiple)
        for answer in options:
            poll.add_answer(text=answer)
        message = await channel.send(poll=poll, allowed_mentions=discord.AllowedMentions.none(), silent=True)
        connection.execute('INSERT INTO community_polls VALUES(?,?,?,?,?,?)', (interaction.guild_id, message.id, channel.id, interaction.user.id, question, time.time()))
        connection.commit()
        put_setting(interaction.guild_id, 'module:polls', '1')
        await reply(interaction, f'Native Discord poll created: {message.jump_url}')

    async def poll_message(interaction, message_id):
        require_guild(interaction)
        if not message_id.isdecimal():
            raise CommunityError('Provide the numeric message ID of a poll created by this bot.')
        rows = service.query('SELECT * FROM community_polls WHERE guild=? AND message=?', (interaction.guild_id, int(message_id)))
        if not rows:
            raise CommunityError('That poll was not created by this bot in this server.')
        record = rows[0]
        channel = interaction.guild.get_channel(record['channel'])
        if not channel or not channel.permissions_for(interaction.user).view_channel:
            raise CommunityError('You cannot access that poll’s channel.')
        message = await channel.fetch_message(record['message'])
        if not message.poll:
            raise CommunityError('The message no longer contains a native poll.')
        return record, message

    @polls.command(name='end', description='End your native Discord poll and finalize its vote count')
    async def poll_end(interaction: discord.Interaction, message_id: str):
        record, message = await poll_message(interaction, message_id)
        if interaction.user.id != record['author'] and not service.moderator(interaction.user):
            raise CommunityError('Only the poll author or staff can end this poll.')
        await message.end_poll()
        await reply(interaction, 'Discord finalized this poll.')

    @polls.command(name='results', description='Read current native poll votes and finalization state')
    async def poll_results(interaction: discord.Interaction, message_id: str):
        record, message = await poll_message(interaction, message_id)
        state = 'Final' if message.poll.is_finalised() else 'Current approximate'
        await reply(interaction, state + ' results:\n' + '\n'.join(f'{a.text}: {a.vote_count}' for a in message.poll.answers))

    @polls.command(name='list', description='List native polls created by this bot in this server')
    async def poll_list(interaction: discord.Interaction):
        require_guild(interaction)
        rows = service.query('SELECT * FROM community_polls WHERE guild=? ORDER BY created DESC LIMIT 20', (interaction.guild_id,))
        visible = [r for r in rows if interaction.guild.get_channel(r['channel']) and interaction.guild.get_channel(r['channel']).permissions_for(interaction.user).view_channel]
        await reply(interaction, '\n'.join(f'{r["message"]}: {r["question"][:80]}' for r in visible) or 'No polls visible to you.')

    @starboard.command(name='channel', description='Select an existing starboard destination and enable it')
    @app_commands.default_permissions(manage_guild=True)
    async def starboard_channel(interaction: discord.Interaction, channel: discord.TextChannel):
        require_admin(interaction)
        put_setting(interaction.guild_id, 'starboard:channel', str(channel.id))
        put_setting(interaction.guild_id, 'module:starboard', '1')
        await reply(interaction, f'Starboard enabled in {channel.mention}. Messages are copied only when channel access is compatible.')

    @starboard.command(name='threshold', description='Set the number of member stars needed for a starboard post')
    @app_commands.default_permissions(manage_guild=True)
    async def threshold(interaction: discord.Interaction, stars: app_commands.Range[int, 1, 100]):
        require_admin(interaction)
        put_setting(interaction.guild_id, 'starboard:threshold', str(stars))
        await reply(interaction, f'Starboard threshold saved: {stars} stars. Existing posts refresh on their next reaction change.')

    @starboard.command(name='emoji', description='Choose the starboard reaction emoji')
    @app_commands.default_permissions(manage_guild=True)
    async def emoji(interaction: discord.Interaction, emoji: str):
        require_admin(interaction)
        if not emoji.strip() or len(emoji) > 100 or '\n' in emoji:
            raise CommunityError('Choose a single Unicode emoji or a Discord custom emoji.')
        put_setting(interaction.guild_id, 'starboard:emoji', emoji.strip())
        await reply(interaction, 'Starboard reaction emoji saved.')

    @starboard.command(name='enabled', description='Enable or disable starboard reaction handling')
    @app_commands.default_permissions(manage_guild=True)
    async def starboard_enabled(interaction: discord.Interaction, enabled: bool):
        require_admin(interaction)
        put_setting(interaction.guild_id, 'module:starboard', '1' if enabled else '0')
        await reply(interaction, 'Starboard ' + ('enabled.' if enabled else 'disabled.'))

    @starboard.command(name='top', description='Show the most-starred messages this server has recorded')
    async def starboard_top(interaction: discord.Interaction):
        require_guild(interaction)
        rows = service.query('SELECT * FROM community_stars WHERE guild=? ORDER BY stars DESC LIMIT 20', (interaction.guild_id,))
        visible = [r for r in rows if interaction.guild.get_channel(r['source']) and interaction.guild.get_channel(r['source']).permissions_for(interaction.user).view_channel]
        await reply(interaction, '\n'.join(f'{r["stars"]} stars · https://discord.com/channels/{interaction.guild_id}/{r["source"]}/{r["message"]}' for r in visible) or 'No starred messages visible to you.')

    @starboard.command(name='remove', description='Remove a recorded starboard repost without deleting the original')
    @app_commands.default_permissions(manage_messages=True)
    async def starboard_remove(interaction: discord.Interaction, original_message_id: str):
        require_staff(interaction)
        if not original_message_id.isdecimal():
            raise CommunityError('Provide the original numeric message ID.')
        rows = service.query('SELECT * FROM community_stars WHERE guild=? AND message=?', (interaction.guild_id, int(original_message_id)))
        if not rows:
            raise CommunityError('This server has no recorded starboard repost for that message.')
        record = rows[0]
        destination = interaction.guild.get_channel(record['destination'])
        if destination and record['posted']:
            message = await destination.fetch_message(record['posted'])
            await message.delete()
        connection.execute('DELETE FROM community_stars WHERE guild=? AND message=?', (interaction.guild_id, int(original_message_id)))
        connection.commit()
        await reply(interaction, 'Starboard repost removed. The original message was preserved.')

    return service
