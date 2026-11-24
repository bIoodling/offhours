"""Discover the implemented interface directly from the registered command tree."""
import discord
from discord import app_commands
from services.configuration import allowed_command, private


class HelpView(discord.ui.View):
    def __init__(self,bot,user,get):
        super().__init__(timeout=300)
        self.user=user
        commands=bot.tree.get_commands()
        groups=[x for x in commands if isinstance(x,app_commands.Group)]
        choices=[('Frequently used commands','standalone')]+[(x.name,x.name) for x in groups]
        for offset in range(0,len(choices),25):
            select=discord.ui.Select(placeholder='Choose a command category',options=[discord.SelectOption(label=label,value=value) for label,value in choices[offset:offset+25]])
            async def callback(i,select=select):
                if i.user.id!=self.user:return await private(i,'Open your own /help menu.')
                value=select.values[0]
                entries=[x for x in commands if not isinstance(x,app_commands.Group)] if value=='standalone' else list(bot.tree.get_command(value).walk_commands())
                lines=[]
                for command in entries:
                    if isinstance(command,app_commands.Group):continue
                    ok,_=allowed_command(i,get,command.qualified_name)
                    if ok:lines.append(f'**/{command.qualified_name}** — {command.description}')
                embed=discord.Embed(title='offhours / '+value,description='\n'.join(lines)[:4000] or 'No enabled commands in this category for your server permissions.',colour=0xD4D4D4)
                embed.set_footer(text='Discord autocomplete shows each command’s parameters. Administrative actions also check actual permissions.')
                await i.response.edit_message(embed=embed,view=self)
            select.callback=callback
            self.add_item(select)


def install(bot,con,get,put):
    @bot.tree.command(name='help',description='Browse the commands available in this server')
    async def help_command(i:discord.Interaction):
        await private(i,'Choose a category. Start with /setup to select this server’s existing channels and roles.',view=HelpView(bot,i.user.id,get))
