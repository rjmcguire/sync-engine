from sqlalchemy import and_, or_, desc, asc, func, bindparam, distinct
from sqlalchemy.orm import subqueryload, contains_eager
from inbox.api.err import InputError
from inbox.api.validation import valid_public_id
from inbox.models import (Contact, Event, Calendar, Message,
                          MessageContactAssociation, Thread,
                          Block, Part, MessageCategory, Category,
                          Metadata)
from inbox.models.event import RecurringEvent
from inbox.sqlalchemy_ext.util import bakery


def _threads_filters(namespace_id, thread_public_id, started_before,
                     started_after, last_message_before, last_message_after,
                     subject):
    filters = [Thread.namespace_id == namespace_id]
    if thread_public_id is not None:
        filters.append(Thread.public_id == thread_public_id)

    if started_before is not None:
        filters.append(Thread.subjectdate < started_before)

    if started_after is not None:
        filters.append(Thread.subjectdate > started_after)

    if last_message_before is not None:
        filters.append(Thread.recentdate < last_message_before)

    if last_message_after is not None:
        filters.append(Thread.recentdate > last_message_after)

    if subject is not None:
        filters.append(Thread.subject == subject)
    return filters


def _threads_subqueries(namespace_id, from_addr, to_addr, cc_addr, bcc_addr,
                        any_email, filename, unread, starred, db_session):
    subqueries = []
    if from_addr is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(MessageContactAssociation).
                          join(Contact).
                          filter(Contact.email_address == from_addr,
                                 Contact.namespace_id == namespace_id,
                                 MessageContactAssociation.field == 'from_addr').subquery())

    if to_addr is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(MessageContactAssociation).
                          join(Contact).
                          filter(Contact.email_address == to_addr,
                                 Contact.namespace_id == namespace_id,
                                 MessageContactAssociation.field == 'to_addr').subquery())

    if cc_addr is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(MessageContactAssociation).
                          join(Contact).
                          filter(Contact.email_address == cc_addr,
                                 Contact.namespace_id == namespace_id,
                                 MessageContactAssociation.field == 'cc_addr').subquery())

    if bcc_addr is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(MessageContactAssociation).
                          join(Contact).
                          filter(Contact.email_address == bcc_addr,
                                 Contact.namespace_id == namespace_id,
                                 MessageContactAssociation.field == 'bcc_addr').subquery())

    if any_email is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(MessageContactAssociation).
                          join(Contact).
                          filter(Contact.email_address.in_(any_email),
                                 Contact.namespace_id == namespace_id).subquery())

    if filename is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          join(Part).
                          join(Block).
                          filter(Block.filename == filename,
                                 Block.namespace_id == namespace_id).subquery())

    if unread is not None:
        read = not unread
        subqueries.append(db_session.query(Message.thread_id).
                          filter(Message.namespace_id == namespace_id,
                                 Message.is_read == read).subquery())

    if starred is not None:
        subqueries.append(db_session.query(Message.thread_id).
                          filter(Message.namespace_id == namespace_id,
                                 Message.is_starred == starred).subquery())
    return subqueries


def _threads_join_category(thread_query, namespace_id, in_):
    if in_ is not None:
        category_filters = [Category.name == in_, Category.display_name == in_]
        try:
            valid_public_id(in_)
            category_filters.append(Category.public_id == in_)
        except InputError:
            pass
        return thread_query.join(Thread.messages). \
            join(Message.messagecategories). \
            join(MessageCategory.category). \
            filter(Category.namespace_id == namespace_id,
                   or_(*category_filters))
    return thread_query


def threads(namespace_id, subject, from_addr, to_addr, cc_addr, bcc_addr,
            any_email, thread_public_id, started_before, started_after,
            last_message_before, last_message_after, filename, in_, unread,
            starred, limit, offset, view, db_session):

    if view == 'count':
        query = db_session.query(func.count(distinct(Thread.id)))
    elif view == 'ids':
        query = db_session.query(Thread.public_id)
    else:
        query = db_session.query(Thread)

    filters = _threads_filters(namespace_id, thread_public_id, started_before,
                              started_after, last_message_before,
                              last_message_after, subject)

    query = _threads_join_category(query, namespace_id, in_)
    query = query.filter(*filters)
    for subquery in _threads_subqueries(namespace_id, from_addr, to_addr,
                                       cc_addr, bcc_addr, any_email, filename,
                                       unread, starred, db_session):
        query = query.filter(Thread.id.in_(subquery))

    if view == 'count':
        return {"count": query.one()[0]}

    # Eager-load some objects in order to make constructing API
    # representations faster.
    if view != 'ids':
        expand = (view == 'expanded')
        query = query.options(*Thread.api_loading_options(expand))

    query = query.order_by(desc(Thread.recentdate)).limit(limit)

    if offset:
        query = query.offset(offset)

    if view == 'ids':
        return [x[0] for x in query.all()]

    return query.all()


def messages_or_drafts(namespace_id, drafts, subject, from_addr, to_addr,
                       cc_addr, bcc_addr, any_email, thread_public_id,
                       started_before, started_after, last_message_before,
                       last_message_after, received_before, received_after,
                       filename, in_, unread, starred, limit, offset, view,
                       db_session):
    # Warning: complexities ahead. This function sets up the query that gets
    # results for the /messages API. It loads from several tables, supports a
    # variety of views and filters, and is performance-critical for the API. As
    # such, it is not super simple.
    #
    # We bake the generated query to avoid paying query compilation overhead on
    # every request. This requires some attention: every parameter that can
    # vary between calls *must* be inserted via bindparam(), or else the first
    # value passed will be baked into the query and reused on each request.
    # Subqueries (on contact tables) can't be properly baked, so we have to
    # call query.spoil() on those code paths.

    param_dict = {
        'namespace_id': namespace_id,
        'drafts': drafts,
        'subject': subject,
        'from_addr': from_addr,
        'to_addr': to_addr,
        'cc_addr': cc_addr,
        'bcc_addr': bcc_addr,
        'any_email': any_email,
        'thread_public_id': thread_public_id,
        'received_before': received_before,
        'received_after': received_after,
        'started_before': started_before,
        'started_after': started_after,
        'last_message_before': last_message_before,
        'last_message_after': last_message_after,
        'filename': filename,
        'in_': in_,
        'unread': unread,
        'starred': starred,
        'limit': limit,
        'offset': offset
    }

    if view == 'count':
        query = bakery(lambda s: s.query(func.count(Message.id)))
    elif view == 'ids':
        query = bakery(lambda s: s.query(Message.public_id))
    else:
        query = bakery(lambda s: s.query(Message))
    query += lambda q: q.join(Thread)
    query += lambda q: q.filter(
        Message.namespace_id == bindparam('namespace_id'),
        Message.is_draft == bindparam('drafts'))

    if subject is not None:
        query += lambda q: q.filter(Message.subject == bindparam('subject'))

    if unread is not None:
        query += lambda q: q.filter(Message.is_read != bindparam('unread'))

    if starred is not None:
        query += lambda q: q.filter(Message.is_starred == bindparam('starred'))

    if thread_public_id is not None:
        query += lambda q: q.filter(
            Thread.public_id == bindparam('thread_public_id'))

    # TODO: deprecate thread-oriented date filters on message endpoints.
    if started_before is not None:
        query += lambda q: q.filter(
            Thread.subjectdate < bindparam('started_before'),
            Thread.namespace_id == bindparam('namespace_id'))

    if started_after is not None:
        query += lambda q: q.filter(
            Thread.subjectdate > bindparam('started_after'),
            Thread.namespace_id == bindparam('namespace_id'))

    if last_message_before is not None:
        query += lambda q: q.filter(
            Thread.recentdate < bindparam('last_message_before'),
            Thread.namespace_id == bindparam('namespace_id'))

    if last_message_after is not None:
        query += lambda q: q.filter(
            Thread.recentdate > bindparam('last_message_after'),
            Thread.namespace_id == bindparam('namespace_id'))

    if received_before is not None:
        query += lambda q: q.filter(
            Message.received_date <= bindparam('received_before'))

    if received_after is not None:
        query += lambda q: q.filter(
            Message.received_date > bindparam('received_after'))

    if to_addr is not None:
        query.spoil()
        to_query = db_session.query(MessageContactAssociation.message_id). \
            join(Contact).filter(
                MessageContactAssociation.field == 'to_addr',
                Contact.email_address == to_addr,
                Contact.namespace_id == bindparam('namespace_id')).subquery()
        query += lambda q: q.filter(Message.id.in_(to_query))

    if from_addr is not None:
        query.spoil()
        from_query = db_session.query(MessageContactAssociation.message_id). \
            join(Contact).filter(
                MessageContactAssociation.field == 'from_addr',
                Contact.email_address == from_addr,
                Contact.namespace_id == bindparam('namespace_id')).subquery()
        query += lambda q: q.filter(Message.id.in_(from_query))

    if cc_addr is not None:
        query.spoil()
        cc_query = db_session.query(MessageContactAssociation.message_id). \
            join(Contact).filter(
                MessageContactAssociation.field == 'cc_addr',
                Contact.email_address == cc_addr,
                Contact.namespace_id == bindparam('namespace_id')).subquery()
        query += lambda q: q.filter(Message.id.in_(cc_query))

    if bcc_addr is not None:
        query.spoil()
        bcc_query = db_session.query(MessageContactAssociation.message_id). \
            join(Contact).filter(
                MessageContactAssociation.field == 'bcc_addr',
                Contact.email_address == bcc_addr,
                Contact.namespace_id == bindparam('namespace_id')).subquery()
        query += lambda q: q.filter(Message.id.in_(bcc_query))

    if any_email is not None:
        query.spoil()
        any_email_query = db_session.query(
            MessageContactAssociation.message_id).join(Contact). \
            filter(Contact.email_address.in_(any_email),
                   Contact.namespace_id == bindparam('namespace_id')). \
            subquery()
        query += lambda q: q.filter(Message.id.in_(any_email_query))

    if filename is not None:
        query += lambda q: q.join(Part).join(Block). \
            filter(Block.filename == bindparam('filename'),
                   Block.namespace_id == bindparam('namespace_id'))

    if in_ is not None:
        query.spoil()
        category_filters = [Category.name == bindparam('in_'),
                            Category.display_name == bindparam('in_')]
        try:
            valid_public_id(in_)
            category_filters.append(Category.public_id == bindparam('in_id'))
            # Type conversion and bindparams interact poorly -- you can't do
            # e.g.
            # query.filter(or_(Category.name == bindparam('in_'),
            #                  Category.public_id == bindparam('in_')))
            # because the binary conversion defined by Category.public_id will
            # be applied to the bound value prior to its insertion in the
            # query. So we define another bindparam for the public_id:
            param_dict['in_id'] = in_
        except InputError:
            pass
        query += lambda q: q.prefix_with('STRAIGHT_JOIN'). \
            join(Message.messagecategories).join(MessageCategory.category). \
            filter(Category.namespace_id == namespace_id,
                   or_(*category_filters))

    if view == 'count':
        res = query(db_session).params(**param_dict).one()[0]
        return {"count": res}

    query += lambda q: q.order_by(desc(Message.received_date))
    query += lambda q: q.limit(bindparam('limit'))
    if offset:
        query += lambda q: q.offset(bindparam('offset'))

    if view == 'ids':
        res = query(db_session).params(**param_dict).all()
        return [x[0] for x in res]

    # Eager-load related attributes to make constructing API representations
    # faster. Note that we don't use the options defined by
    # Message.api_loading_options() here because we already have a join to the
    # thread table. We should eventually try to simplify this.
    query += lambda q: q.options(
        contains_eager(Message.thread),
        subqueryload(Message.messagecategories).joinedload('category'),
        subqueryload(Message.parts).joinedload(Part.block),
        subqueryload(Message.events))

    prepared = query(db_session).params(**param_dict)
    return prepared.all()


def files(namespace_id, message_public_id, filename, content_type,
          limit, offset, view, db_session):

    if view == 'count':
        query = db_session.query(func.count(Block.id))
    elif view == 'ids':
        query = db_session.query(Block.public_id)
    else:
        query = db_session.query(Block)

    query = query.filter(Block.namespace_id == namespace_id)

    # limit to actual attachments (no content-disposition == not a real
    # attachment)
    query = query.outerjoin(Part)
    query = query.filter(or_(Part.id.is_(None),
                             Part.content_disposition.isnot(None)))

    if content_type is not None:
        query = query.filter(or_(Block._content_type_common == content_type,
                                 Block._content_type_other == content_type))

    if filename is not None:
        query = query.filter(Block.filename == filename)

    # Handle the case of fetching attachments on a particular message.
    if message_public_id is not None:
        query = query.join(Message) \
            .filter(Message.public_id == message_public_id)

    if view == 'count':
        return {"count": query.one()[0]}

    query = query.order_by(asc(Block.id)).distinct().limit(limit)

    if offset:
        query = query.offset(offset)

    if view == 'ids':
        return [x[0] for x in query.all()]
    else:
        return query.all()


def filter_event_query(query, event_cls, namespace_id, event_public_id,
                       calendar_public_id, title, description, location, busy):

    query = query.filter(event_cls.namespace_id == namespace_id)

    if event_public_id:
        query = query.filter(event_cls.public_id == event_public_id)

    if calendar_public_id is not None:
        query = query.join(Calendar). \
            filter(Calendar.public_id == calendar_public_id,
                   Calendar.namespace_id == namespace_id)

    if title is not None:
        query = query.filter(event_cls.title.like(u'%{}%'.format(title)))

    if description is not None:
        query = query.filter(event_cls.description.like(u'%{}%'
                                                        .format(description)))

    if location is not None:
        query = query.filter(event_cls.location.like(u'%{}%'.format(location)))

    if busy is not None:
        query = query.filter(event_cls.busy == busy)

    query = query.filter(event_cls.source == 'local')

    return query


def recurring_events(filters, starts_before, starts_after, ends_before,
                     ends_after, db_session, show_cancelled=False):
    # Expands individual recurring events into full instances.
    # If neither starts_before or ends_before is given, the recurring range
    # defaults to now + 1 year (see events/recurring.py)

    recur_query = db_session.query(RecurringEvent)
    recur_query = filter_event_query(recur_query, RecurringEvent, *filters)

    if show_cancelled is False:
        recur_query = recur_query.filter(RecurringEvent.status != 'cancelled')

    before_criteria = []
    if starts_before:
        before_criteria.append(RecurringEvent.start < starts_before)
    if ends_before:
        # start < end, so event start < ends_before
        before_criteria.append(RecurringEvent.start < ends_before)
    recur_query = recur_query.filter(and_(*before_criteria))
    after_criteria = []
    if starts_after:
        after_criteria.append(or_(RecurringEvent.until > starts_after,
                                  RecurringEvent.until == None))  # noqa
    if ends_after:
        after_criteria.append(or_(RecurringEvent.until > ends_after,
                                  RecurringEvent.until == None))  # noqa

    recur_query = recur_query.filter(and_(*after_criteria))

    recur_instances = []

    for r in recur_query:
        # the occurrences check only checks starting timestamps
        if ends_before and not starts_before:
            starts_before = ends_before - r.length
        if ends_after and not starts_after:
            starts_after = ends_after - r.length
        instances = r.all_events(start=starts_after, end=starts_before)
        recur_instances.extend(instances)

    return recur_instances


def events(namespace_id, event_public_id, calendar_public_id, title,
           description, location, busy, starts_before, starts_after,
           ends_before, ends_after, limit, offset, view,
           expand_recurring, show_cancelled, db_session):

    query = db_session.query(Event)

    if not expand_recurring:
        if view == 'count':
            query = db_session.query(func.count(Event.id))
        elif view == 'ids':
            query = db_session.query(Event.public_id)

    filters = [namespace_id, event_public_id, calendar_public_id,
               title, description, location, busy]
    query = filter_event_query(query, Event, *filters)

    event_criteria = []

    if starts_before is not None:
        event_criteria.append(Event.start < starts_before)

    if starts_after is not None:
        event_criteria.append(Event.start > starts_after)

    if ends_before is not None:
        event_criteria.append(Event.end < ends_before)

    if ends_after is not None:
        event_criteria.append(Event.end > ends_after)

    if not show_cancelled:
        if expand_recurring:
            event_criteria.append(Event.status != 'cancelled')
        else:
            # It doesn't make sense to hide cancelled events
            # when we're not expanding recurring events,
            # so don't do it.
            # We still need to show cancelled recurringevents
            # for those users who want to do event expansion themselves.
            event_criteria.append(
                (Event.discriminator == 'recurringeventoverride') |
                ((Event.status != 'cancelled') & (Event.discriminator !=
                                                  'recurringeventoverride')))

    event_predicate = and_(*event_criteria)
    query = query.filter(event_predicate)

    if expand_recurring:
        expanded = recurring_events(filters, starts_before, starts_after,
                                    ends_before, ends_after, db_session,
                                    show_cancelled=show_cancelled)

        # Combine non-recurring events with expanded recurring ones
        all_events = query.filter(Event.discriminator == 'event').all() + \
            expanded

        if view == 'count':
            return {"count": len(all_events)}

        all_events = sorted(all_events, key=lambda e: e.start)
        if limit:
            offset = offset or 0
            all_events = all_events[offset:offset + limit]
    else:
        if view == 'count':
            return {"count": query.one()[0]}
        query = query.order_by(asc(Event.start)).limit(limit)
        if offset:
            query = query.offset(offset)
        # Eager-load some objects in order to make constructing API
        # representations faster.
        all_events = query.all()

    if view == 'ids':
        return [x[0] for x in all_events]
    else:
        return all_events


def messages_for_contact_scores(db_session, namespace_id, starts_after=None):
    query = (db_session.query(
        Message.to_addr, Message.cc_addr, Message.bcc_addr,
        Message.id, Message.received_date.label('date'))
        .join(MessageCategory.message)
        .join(MessageCategory.category)
        .filter(Message.namespace_id == namespace_id)
        .filter(Category.name == 'sent')
        .filter(~Message.is_draft)
        .filter(Category.namespace_id == namespace_id))

    if starts_after:
        query = query.filter(Message.received_date > starts_after)

    return query.all()


def metadata(namespace_id, app_id, view, limit, offset,
             db_session):

    if view == 'count':
        query = db_session.query(func.count(Metadata.id))
    elif view == 'ids':
        query = db_session.query(Metadata.object_public_id)
    else:
        query = db_session.query(Metadata)

    filters = [Metadata.namespace_id == namespace_id,
               Metadata.value.isnot(None)]
    if app_id is not None:
        filters.append(Metadata.app_id == app_id)

    query = query.filter(*filters)
    if view == 'count':
        return {"count": query.scalar()}

    query = query.order_by(desc(Metadata.id)).limit(limit)

    if offset:
        query = query.offset(offset)

    if view == 'ids':
        return [x[0] for x in query.all()]

    return query.all()


def metadata_for_app(app_id, limit, last, query_value, query_type, db_session):
    if app_id is None:
        raise ValueError('Must specify an app_id')

    query = db_session.query(Metadata).filter(Metadata.app_id == app_id)
    if last is not None:
        query = query.filter(Metadata.id > last)

    if query_type is not None:
        if query_type not in METADATA_QUERY_OPERATORS:
            raise ValueError(
                'Invalid query operator for metadata query_type. Must be '
                'one of {}'.format(', '.join(METADATA_QUERY_OPERATORS.keys())))
        operator_filter = METADATA_QUERY_OPERATORS[query_type](query_value)
        query = query.filter(operator_filter)

    query = query.order_by(asc(Metadata.id)).limit(limit)
    return query.all()

METADATA_QUERY_OPERATORS = {
    '>': lambda v: Metadata.queryable_value > v,
    '>=': lambda v: Metadata.queryable_value >= v,
    '<': lambda v: Metadata.queryable_value < v,
    '<=': lambda v: Metadata.queryable_value <= v,
    '==': lambda v: Metadata.queryable_value == v,
    '!=': lambda v: Metadata.queryable_value != v,
}
