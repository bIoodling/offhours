"""Reusable server-owned Discord embeds with native editing and attachment storage."""
import io
import json
import re

import discord
from discord import app_commands

from cogs.panels import safe_image


async def reply(i, text='', **kwargs):
    kwargs.setdefault('ephemeral', True)
    kwargs.setdefault('allowed_mentions', discord.AllowedMentions.none())
    if i.response.is_done():
        return await i.followup.send(text[:1900], **kwargs)
    return await i.response.send_message(text[:1900], **kwargs)


class EmbedStore:
    def __init__(self, db):
        self.db = db
        db.execute('CREATE TABLE IF NOT EXISTS embed_templates(guild INTEGER,name TEXT,data TEXT,PRIMARY KEY(guild,name))')
        db.execute('CREATE TABLE IF NOT EXISTS embed_assets(guild INTEGER,name TEXT,data BLOB,filename TEXT,PRIMARY KEY(guild,name))')
        db.commit()

    def name(self, name):
        name = name.strip().lower()
        if not re.fullmatch(r'[a-z0-9_-]{1,50}', name):
            raise ValueError('Use a template name containing 1–50 lowercase letters, numbers, hyphens or underscores.')
        return name

    def get(self, guild, name):
        name = self.name(name)
        row = self.db.execute('SELECT data FROM embed_templates WHERE guild=? AND name=?', (guild,name)).fetchone()
        if not row:
            raise ValueError('No embed with that name is saved in this server.')
        return json.loads(row[0])

    def validate(self, data):
        if not isinstance(data,dict) or not set(data) <= {'title','description','color','fields','footer','author','thumbnail','image','timestamp','url','type'}:
            raise ValueError('This JSON contains unsupported embed properties.')
        if data.get('type','rich') != 'rich':
            raise ValueError('Only rich Discord embed templates are supported.')
        for key, maximum in [('title',256),('description',4096)]:
            if key in data and (not isinstance(data[key],str) or len(data[key]) > maximum):
                raise ValueError(f'{key} exceeds Discord’s length limit.')
        if 'color' in data and (type(data['color']) is not int or not 0 <= data['color'] <= 0xFFFFFF):
            raise ValueError('Embed color must be an integer from 0 through 16777215.')
        fields = data.get('fields',[])
        if not isinstance(fields,list) or len(fields)>25:
            raise ValueError('An embed may contain up to 25 fields.')
        for field in fields:
            if not isinstance(field,dict) or not set(field)<={'name','value','inline'} or not isinstance(field.get('name'),str) or not isinstance(field.get('value'),str) or not 1<=len(field['name'])<=256 or not 1<=len(field['value'])<=1024 or type(field.get('inline',False)) is not bool:
                raise ValueError('Each field needs a name (1–256 characters), value (1–1024 characters) and optional inline boolean.')
        for key, allowed in [('author',{'name','url','icon_url'}),('footer',{'text','icon_url'}),('image',{'url'}),('thumbnail',{'url'})]:
            if key in data:
                value=data[key]
                if not isinstance(value,dict) or not set(value)<=allowed or any(not isinstance(x,str) for x in value.values()):
                    raise ValueError(f'Invalid {key} properties.')
                for name in ('url','icon_url'):
                    if value.get(name) and not value[name].startswith('attachment://'):
                        if '{' in value[name] or '}' in value[name]:
                            raise ValueError('Embed template image links must be literal HTTPS URLs; use the panel editor for member variables.')
                        safe_image(value[name])
                if key=='author' and not 1<=len(value.get('name',''))<=256:
                    raise ValueError('Author name must be 1–256 characters.')
                if key=='footer' and not 1<=len(value.get('text',''))<=2048:
                    raise ValueError('Footer text must be 1–2048 characters.')
        if data.get('url'):
            if not isinstance(data['url'],str) or '{' in data['url'] or '}' in data['url']:
                raise ValueError('Embed links must be literal HTTPS URLs.')
            safe_image(data['url'])
        if data.get('timestamp'):
            if not isinstance(data['timestamp'],str) or discord.utils.parse_time(data['timestamp']) is None:
                raise ValueError('Invalid ISO timestamp.')
        result=discord.Embed.from_dict(data)
        if len(result)>6000:
            raise ValueError('An embed’s combined text must not exceed 6000 characters.')
        if not result:
            raise ValueError('An embed must contain some visible content.')
        return result

    def save(self,guild,name,data):
        name=self.name(name)
        embed=self.validate(data)
        if not self.db.execute('SELECT 1 FROM embed_templates WHERE guild=? AND name=?',(guild,name)).fetchone() and self.db.execute('SELECT COUNT(*) FROM embed_templates WHERE guild=?',(guild,)).fetchone()[0]>=100:
            raise ValueError('This server already has 100 saved embed templates.')
        self.db.execute('INSERT INTO embed_templates(guild,name,data) VALUES(?,?,?) ON CONFLICT(guild,name) DO UPDATE SET data=excluded.data',(guild,name,json.dumps(embed.to_dict())))
        self.db.commit()
        return embed

    def files(self,guild,name):
        row=self.db.execute('SELECT data,filename FROM embed_assets WHERE guild=? AND name=?',(guild,self.name(name))).fetchone()
        return [discord.File(io.BytesIO(row[0]),filename=row[1])] if row else []


class DeleteTemplate(discord.ui.View):
    def __init__(self,store,i,name):
        super().__init__(timeout=120)
        self.store,self.guild,self.actor,self.name=store,i.guild_id,i.user.id,name

    async def interaction_check(self,i):
        if i.guild_id!=self.guild or i.user.id!=self.actor or not i.user.guild_permissions.manage_messages:
            await reply(i,'Only the moderator who requested deletion can confirm it.')
            return False
        return True

    @discord.ui.button(label='Delete saved template',style=discord.ButtonStyle.danger)
    async def confirm(self,i,button):
        self.store.db.execute('DELETE FROM embed_templates WHERE guild=? AND name=?',(self.guild,self.name))
        self.store.db.execute('DELETE FROM embed_assets WHERE guild=? AND name=?',(self.guild,self.name))
        self.store.db.commit()
        self.stop()
        await i.response.edit_message(content='Saved embed template deleted. Published messages remain.',view=None)

    @discord.ui.button(label='Cancel',style=discord.ButtonStyle.secondary)
    async def cancel(self,i,button):
        self.stop()
        await i.response.edit_message(content='Deletion cancelled.',view=None)


def install(bot,connection,get_setting=None,put_setting=None):
    store=EmbedStore(connection)
    group=app_commands.Group(name='embed',description='Save, edit and publish reusable server embeds',guild_only=True,
        default_permissions=discord.Permissions(manage_messages=True))

    def require(i):
        if not i.guild or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.manage_messages:
            raise ValueError('Manage Messages permission is required for embed templates.')

    async def error(i,exc):
        original=getattr(exc,'original',exc)
        if isinstance(original,ValueError):
            return await reply(i,str(original))
        if isinstance(original,discord.HTTPException):
            return await reply(i,'Discord rejected the embed operation. Check channel permissions and embed limits.')
        raise exc
    group.on_error=error

    @group.command(name='create',description='Create a named reusable embed template')
    async def create(i:discord.Interaction,name:str,title:str,description:str='',colour:str='D4D4D4'):
        require(i)
        if not re.fullmatch(r'#?[a-fA-F0-9]{6}',colour):
            raise ValueError('Use a six-digit hex colour.')
        if connection.execute('SELECT 1 FROM embed_templates WHERE guild=? AND name=?',(i.guild_id,store.name(name))).fetchone():
            raise ValueError('That name is already saved. Use /embed edit or choose another name.')
        embed=store.save(i.guild_id,name,dict(title=title,description=description,color=int(colour.lstrip('#'),16)))
        await reply(i,'Embed template saved.',embed=embed)

    @group.command(name='edit',description='Change a template’s title, description or colour')
    async def edit(i:discord.Interaction,name:str,title:str=None,description:str=None,colour:str=None):
        require(i)
        data=store.get(i.guild_id,name)
        if all(v is None for v in (title,description,colour)):
            raise ValueError('Provide at least one field to change.')
        if title is not None:data['title']=title
        if description is not None:data['description']=description
        if colour is not None:
            if not re.fullmatch(r'#?[a-fA-F0-9]{6}',colour):raise ValueError('Use a six-digit hex colour.')
            data['color']=int(colour.lstrip('#'),16)
        await reply(i,'Embed template updated.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='send',description='Publish a saved embed in a selected existing channel')
    async def send(i:discord.Interaction,name:str,channel:discord.TextChannel):
        require(i)
        if not channel.permissions_for(i.user).send_messages or not channel.permissions_for(i.user).embed_links:
            raise ValueError('You need Send Messages and Embed Links in the destination.')
        embed=store.validate(store.get(i.guild_id,name))
        await i.response.defer(ephemeral=True)
        message=await channel.send(embed=embed,files=store.files(i.guild_id,name),allowed_mentions=discord.AllowedMentions.none(),silent=True)
        await reply(i,f'Embed published: {message.jump_url}')

    @group.command(name='preview',description='Preview a saved template privately')
    async def preview(i:discord.Interaction,name:str):
        require(i)
        await reply(i,'Private template preview.',embed=store.validate(store.get(i.guild_id,name)),files=store.files(i.guild_id,name))

    @group.command(name='list',description='List this server’s saved embed templates')
    async def list_templates(i:discord.Interaction):
        require(i)
        rows=connection.execute('SELECT name FROM embed_templates WHERE guild=? ORDER BY name LIMIT 100',(i.guild_id,)).fetchall()
        await reply(i,', '.join(x[0] for x in rows) or 'No saved embed templates.')

    @group.command(name='delete',description='Delete a saved template after a private confirmation')
    async def delete(i:discord.Interaction,name:str):
        require(i)
        store.get(i.guild_id,name)
        await reply(i,f'Delete saved template {store.name(name)}?',view=DeleteTemplate(store,i,store.name(name)))

    @group.command(name='clone',description='Copy a saved template under a new name')
    async def clone(i:discord.Interaction,source:str,name:str):
        require(i)
        if connection.execute('SELECT 1 FROM embed_templates WHERE guild=? AND name=?',(i.guild_id,store.name(name))).fetchone():
            raise ValueError('The new name is already in use.')
        embed=store.save(i.guild_id,name,store.get(i.guild_id,source))
        connection.execute('INSERT OR REPLACE INTO embed_assets(guild,name,data,filename) SELECT guild,?,data,filename FROM embed_assets WHERE guild=? AND name=?',(store.name(name),i.guild_id,store.name(source)))
        connection.commit()
        await reply(i,'Template copied.',embed=embed,files=store.files(i.guild_id,name))

    @group.command(name='export',description='Export a reusable embed as JSON privately')
    async def export(i:discord.Interaction,name:str):
        require(i)
        data=json.dumps(store.get(i.guild_id,name),indent=2,ensure_ascii=False).encode()
        await reply(i,'Embed JSON attached.',file=discord.File(io.BytesIO(data),filename=store.name(name)+'.json'))

    @group.command(name='import',description='Import a Discord embed JSON attachment with strict validation')
    async def import_template(i:discord.Interaction,name:str,file:discord.Attachment):
        require(i)
        if file.size>64000:raise ValueError('The JSON attachment must be under 64 KB.')
        await i.response.defer(ephemeral=True)
        try:data=json.loads((await file.read()).decode('utf-8'))
        except (ValueError,UnicodeDecodeError):raise ValueError('Attach a valid UTF-8 JSON embed object.') from None
        embed=store.save(i.guild_id,name,data)
        await reply(i,'Embed JSON imported.',embed=embed)

    @group.command(name='copy',description='Save an embed from a message you can read')
    async def copy_message(i:discord.Interaction,channel:discord.TextChannel,message_id:str,name:str):
        require(i)
        permissions=channel.permissions_for(i.user)
        if not permissions.view_channel or not permissions.read_message_history or not message_id.isdecimal():
            raise ValueError('You need access to this channel’s history and a numeric message ID.')
        await i.response.defer(ephemeral=True)
        message=await channel.fetch_message(int(message_id))
        if not message.embeds:raise ValueError('The selected message has no embed.')
        data=message.embeds[0].to_dict()
        # Proxy URLs and provider/video metadata are Discord-generated, not template inputs.
        data={k:v for k,v in data.items() if k in {'title','description','color','fields','footer','author','thumbnail','image','timestamp','url'}}
        for field in ('author','footer','thumbnail','image'):
            if field in data:data[field]={k:v for k,v in data[field].items() if k in {'name','text','url','icon_url'}}
        await reply(i,'Embed saved from the visible message.',embed=store.save(i.guild_id,name,data))

    @group.command(name='author',description='Set a saved embed’s author text and optional icon')
    async def author(i:discord.Interaction,name:str,text:str,icon:str=''):
        require(i)
        data=store.get(i.guild_id,name)
        if icon:safe_image(icon)
        data['author']={'name':text,**({'icon_url':icon} if icon else {})}
        await reply(i,'Author saved.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='footer',description='Set a saved embed’s footer text and optional icon')
    async def footer(i:discord.Interaction,name:str,text:str,icon:str=''):
        require(i)
        data=store.get(i.guild_id,name)
        if icon:safe_image(icon)
        data['footer']={'text':text,**({'icon_url':icon} if icon else {})}
        await reply(i,'Footer saved.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='image',description='Store an image attachment or public HTTPS image on a template')
    async def image(i:discord.Interaction,name:str,url:str='',attachment:discord.Attachment=None):
        require(i)
        data=store.get(i.guild_id,name)
        if attachment:
            if attachment.content_type not in ('image/png','image/jpeg','image/webp','image/gif') or attachment.size>3*1024*1024:
                raise ValueError('Attach a PNG, JPEG, WebP or GIF under 3 MB.')
            await i.response.defer(ephemeral=True)
            extension={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif'}[attachment.content_type]
            filename='embed-image.'+extension
            connection.execute('INSERT INTO embed_assets(guild,name,data,filename) VALUES(?,?,?,?) ON CONFLICT(guild,name) DO UPDATE SET data=excluded.data,filename=excluded.filename',
                (i.guild_id,store.name(name),await attachment.read(),filename))
            connection.commit()
            data['image']={'url':'attachment://'+filename}
        else:
            if not url:raise ValueError('Provide an image attachment or HTTPS URL.')
            data['image']={'url':safe_image(url)}
        await reply(i,'Image saved.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='thumbnail',description='Set a saved embed’s thumbnail to a public HTTPS image')
    async def thumbnail(i:discord.Interaction,name:str,url:str):
        require(i)
        data=store.get(i.guild_id,name);data['thumbnail']={'url':safe_image(url)}
        await reply(i,'Thumbnail saved.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='timestamp',description='Show or hide the saved embed’s timestamp')
    async def timestamp(i:discord.Interaction,name:str,enabled:bool):
        require(i)
        data=store.get(i.guild_id,name)
        if enabled:data['timestamp']=discord.utils.utcnow().isoformat()
        else:data.pop('timestamp',None)
        await reply(i,'Timestamp updated.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='field-add',description='Add a named field to a saved embed')
    async def field_add(i:discord.Interaction,name:str,label:str,value:str,inline:bool=False):
        require(i)
        data=store.get(i.guild_id,name);data.setdefault('fields',[]).append(dict(name=label,value=value,inline=inline))
        await reply(i,'Field added.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='field-remove',description='Remove one numbered field from a saved embed')
    async def field_remove(i:discord.Interaction,name:str,number:app_commands.Range[int,1,25]):
        require(i)
        data=store.get(i.guild_id,name)
        if number>len(data.get('fields',[])):raise ValueError('That field number does not exist.')
        data['fields'].pop(number-1)
        await reply(i,'Field removed.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    @group.command(name='fields-clear',description='Remove all fields from a saved embed')
    async def fields_clear(i:discord.Interaction,name:str):
        require(i)
        data=store.get(i.guild_id,name);data.pop('fields',None)
        await reply(i,'Fields cleared.',embed=store.save(i.guild_id,name,data),files=store.files(i.guild_id,name))

    bot.tree.add_command(group)
    return store
