from __future__ import absolute_import

import collections
import glob
import itertools
import sys
from datetime import datetime

from flask import abort, g, render_template, request, redirect, Blueprint, flash, url_for, current_app, make_response, send_from_directory
from werkzeug.contrib.atom import AtomFeed
from flask_login import login_required, current_user

from realms.version import __version__
from realms.lib.util import to_canonical, remove_ext, gravatar_url
from .models import PageNotFound

from gtts import gTTS
from bs4 import BeautifulSoup

blueprint = Blueprint('wiki', __name__, template_folder='templates',
                      static_folder='static', static_url_path='/static/wiki')


@blueprint.route("/_commit/<sha>/<path:name>")
def commit(name, sha):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()

    cname = to_canonical(name)

    data = g.current_wiki.get_page(cname, sha=sha.decode())

    if not data:
        abort(404)

    partials = _partials(data.imports, sha=sha.decode())

    return render_template('wiki/page.html', name=name, page=data, commit=sha, partials=partials)


@blueprint.route(r"/_compare/<path:name>/<regex('\w+'):fsha><regex('\.{2,3}'):dots><regex('\w+'):lsha>")
def compare(name, fsha, dots, lsha):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()

    diff = g.current_wiki.get_page(name, sha=lsha).compare(fsha)
    return render_template('wiki/compare.html',
                           name=name, diff=diff, old=fsha, new=lsha)


@blueprint.route("/_revert", methods=['POST'])
@login_required
def revert():
    cname = to_canonical(request.form.get('name'))
    commit = request.form.get('commit')
    message = request.form.get('message', "Reverting %s" % cname)

    if not current_app.config.get('ALLOW_ANON') and current_user.is_anonymous:
        return dict(error=True, message="Anonymous posting not allowed"), 403

    if cname in current_app.config.get('WIKI_LOCKED_PAGES'):
        return dict(error=True, message="Page is locked"), 403

    try:
        sha = g.current_wiki.get_page(cname).revert(commit,
                                                    message=message,
                                                    username=current_user.username,
                                                    email=current_user.email)
    except PageNotFound as e:
        return dict(error=True, message=e.message), 404

    if sha:
        flash("Page reverted")

    return dict(sha=sha.decode())


@blueprint.route("/_history/<path:name>")
def history(name):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()
    return render_template('wiki/history.html', name=name)


@blueprint.route("/_feed/<path:name>")
def feed(name):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()
    cname = to_canonical(name)
    wiki_name = current_app.config['SITE_TITLE']
    start = 0
    length = int(request.args.get('length', 20))

    the_feed = AtomFeed(
        title="{} - Recent changes for page '{}'".format(wiki_name, cname),
        url=url_for('wiki.page', name=cname, _external=True),
        id="{}_pagefeed_{}".format(to_canonical(wiki_name), cname),
        feed_url=url_for('wiki.feed', name=cname, _external=True),
        generator=("Realms wiki", 'https://github.com/scragg0x/realms-wiki', __version__)
    )

    page = g.current_wiki.get_page(cname)
    items = list(itertools.islice(page.history, start, start + length))  # type: list[dict]

    for item in items:
        the_feed.add(
            title="Commit '{}'".format(item['sha']),
            content=item['message'],
            url=url_for('wiki.commit', name=name, sha=item['sha'], _external=True),
            id="{}/{}".format(item['sha'], cname),
            author=item['author'],
            updated=datetime.fromtimestamp(item['time'])
        )

    response = make_response((the_feed.to_string(), {'Content-type': 'application/atom+xml; charset=utf-8'}))
    response.add_etag()
    return response.make_conditional(request)


@blueprint.route("/_history_data/<path:name>")
def history_data(name):
    """Ajax provider for paginated history data."""
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()
    draw = int(request.args.get('draw', 0))
    start = int(request.args.get('start', 0))
    length = int(request.args.get('length', 10))
    page = g.current_wiki.get_page(name)
    items = list(itertools.islice(page.history, start, start + length))     # type: list[dict]
    for item in items:
        item['gravatar'] = gravatar_url(item['author_email'])
        item['DT_RowId'] = item['sha']
        date = datetime.fromtimestamp(item['time'])
        item['date'] = date.strftime(current_app.config.get('DATETIME_FORMAT', '%b %d, %Y %I:%M %p'))
        item['link'] = url_for('.commit', name=name, sha=item['sha'])
    total_records, hist_complete = page.history_cache
    if not hist_complete:
        # Force datatables to fetch more data when it gets to the end
        total_records += 1
    return {
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': total_records,
        'data': items,
        'fully_loaded': hist_complete
    }


@blueprint.route("/_edit/<path:name>")
@login_required
def edit(name):
    cname = to_canonical(name)
    page = g.current_wiki.get_page(cname)

    if not page:
        # Page doesn't exist
        return redirect(url_for('wiki.create', name=cname))

    g.assets['js'].append('editor.js')
    return render_template('wiki/edit.html',
                           name=cname,
                           content=page.data.decode(),
                           # TODO: Remove this? See #148
                           info=next(page.history),
                           sha=page.sha)


def _partials(imports, sha='HEAD'):
    page_queue = collections.deque(imports)
    partials = collections.OrderedDict()
    while page_queue:
        page_name = page_queue.popleft()
        if page_name in partials:
            continue
        page = g.current_wiki.get_page(page_name, sha=sha.decode())
        try:
            partials[page_name] = page.data
        except KeyError:
            partials[page_name] = "`Error importing wiki page '{0}'`".format(page_name)
            continue
        page_queue.extend(page.imports)
    # We want to retain the order (and reverse it) so that combining metadata from the imports works
    # nested list for python >3.5 compatibility
    return list(reversed(list(partials.items())))


@blueprint.route("/_partials")
def partials():
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()
    return {'partials': _partials(request.args.getlist('imports[]'))}


@blueprint.route('/_sound/<path:path>')
def sound(path):
    return send_from_directory(filename=path, directory='/tmp/')

@blueprint.route("/_create/", defaults={'name': None})
@blueprint.route("/_create/<path:name>")
@login_required
def create(name):
    cname = to_canonical(name) if name else ""
    if cname and g.current_wiki.get_page(cname):
        # Page exists, edit instead
        return redirect(url_for('wiki.edit', name=cname))

    g.assets['js'].append('editor.js')
    return render_template('wiki/edit.html',
                           name=cname,
                           content="",
                           info={})


def _get_subdir(path, depth):
    parts = path.split('/', depth)
    if len(parts) > depth:
        return parts[-2]


def _tree_index(items, path=""):
    depth = len(path.split("/"))
    items = sorted(items, key=lambda x: x['name'])
    for subdir, items in itertools.groupby(items, key=lambda x: _get_subdir(x['name'], depth)):
        if not subdir:
            for item in items:
                yield dict(item, dir=False)
        else:
            size = 0
            ctime = sys.maxint
            mtime = 0
            for item in items:
                size += item['size']
                ctime = min(item['ctime'], ctime)
                mtime = max(item['mtime'], mtime)
            yield dict(name=path + subdir + "/",
                       mtime=mtime,
                       ctime=ctime,
                       size=size,
                       dir=True)


@blueprint.route("/_index", defaults={"path": ""})
@blueprint.route("/_index/<path:path>")
def index(path):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()

    items = g.current_wiki.get_index()
    if path:
        path = to_canonical(path) + "/"
        items = filter(lambda x: x['name'].startswith(path), items)
    if not request.args.get('flat', '').lower() in ['yes', '1', 'true']:
        items = _tree_index(items, path=path)

    return render_template('wiki/index.html', index=items, path=path)

@blueprint.route("/_bulkadd")
def bulkadd():
    from bs4 import BeautifulSoup
    num_articles = 100
    for name in glob.glob("/data/html/*.html"):
        print("added article", name)
        with open(name, "r") as f:
            soup = BeautifulSoup(f)
            titles = soup.findAll("title")
            title = titles[0].text
            cname = to_canonical(title) if title else ""
            if num_articles <= 0:
                break
            g.current_wiki.get_page(cname).write(f.read(),
                                                 message="automatically added from arXiV",
                                                 username=current_user.username,
                                                 email=current_user.email)
            num_articles -= 1

@blueprint.route("/<path:name>", methods=['POST', 'PUT', 'DELETE'])
@login_required
def page_write(name):
    cname = to_canonical(name)

    if not cname:
        return dict(error=True, message="Invalid name")

    if not current_app.config.get('ALLOW_ANON') and current_user.is_anonymous:
        return dict(error=True, message="Anonymous posting not allowed"), 403

    if request.method == 'POST':
        # Create
        if cname in current_app.config.get('WIKI_LOCKED_PAGES'):
            return dict(error=True, message="Page is locked"), 403

        sha = g.current_wiki.get_page(cname).write(request.form['content'],
                                                   message=request.form['message'],
                                                   username=current_user.username,
                                                   email=current_user.email)

    elif request.method == 'PUT':
        edit_cname = to_canonical(request.form['name'])

        if edit_cname in current_app.config.get('WIKI_LOCKED_PAGES'):
            return dict(error=True, message="Page is locked"), 403

        if edit_cname != cname:
            g.current_wiki.get_page(cname).rename(edit_cname)

        sha = g.current_wiki.get_page(edit_cname).write(request.form['content'],
                                                        message=request.form['message'],
                                                        username=current_user.username,
                                                        email=current_user.email)

        return dict(sha=sha.decode())

    elif request.method == 'DELETE':
        # DELETE
        if cname in current_app.config.get('WIKI_LOCKED_PAGES'):
            return dict(error=True, message="Page is locked"), 403

        sha = g.current_wiki.get_page(cname).delete(username=current_user.username,
                                                    email=current_user.email)

    return dict(sha=sha.decode())

def create_sound_files(html):
    # import IPython
    # IPython.embed()
    i = 0
    soup = BeautifulSoup(html)
    for heading in soup.findAll("h1"):
        print("heading", heading)
        text = []
        for child in heading.nextSiblingGenerator():
            if child.name == "p":
                text.append(child.text)
            if child.name == "h1":
                break
        # if text != []:
        #     tts = gTTS(text="\n\n".join(text), lang='en')
        #     tts.save("/tmp/" + str(i) + ".mp3")
        i += 1

@blueprint.route("/", defaults={'name': 'home'})
@blueprint.route("/<path:name>")
def page(name):
    if current_app.config.get('PRIVATE_WIKI') and current_user.is_anonymous:
        return current_app.login_manager.unauthorized()

    cname = to_canonical(name)
    if cname != name:
        return redirect(url_for('wiki.page', name=cname))

    data = g.current_wiki.get_page(cname)

    if data:
        # m = markdown.markdown(data.data)
        # tts = gTTS(text=m, lang='en')
        # tts.save("/tmp/test.mp3")
        # print("XXX", m)
        create_sound_files(data.data)
        return render_template('wiki/page.html', name=cname, page=data, partials=_partials(data.imports))
    else:
        return redirect(url_for('wiki.create', name=cname))
