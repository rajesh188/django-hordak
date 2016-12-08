from django.db import models
from django.utils import timezone
from django.db import transaction as db_transaction
from django_smalluuid.models import SmallUUIDField, uuid_default
from djmoney.models.fields import MoneyField

from mptt.models import MPTTModel, TreeForeignKey, TreeManager
from model_utils import Choices

from hordak import defaults
from hordak import exceptions

#: Debit
DEBIT = 'debit'
#: Credit
CREDIT = 'credit'


class AccountQuerySet(models.QuerySet):

    def net_balance(self, raw=False):
        return sum(account.balance(raw) for account in self)


class AccountManager(TreeManager):

    def get_by_natural_key(self, uuid):
        return self.get(uuid=uuid)


class Account(MPTTModel):
    """ Represents an account

    An account may have a parent, and may have zero or more children. Only root
    accounts can have a type, all child accounts are assumed to have the same
    type as their parent.

    An account's balance is calculated as the sum of all of the transaction Leg's
    referencing the account.

    Attributes:

        uuid (SmallUUID): UUID for account. Use to prevent leaking of IDs (if desired).
        name (str): Name of the account. Required.
        parent (Account|None): Parent account, nonen if root account
        code (str): Account code. Must combine with account codes of parent
            accounts to get fully qualified account code.
        type (str): Type of account as defined by :attr:`Account.TYPES`. Can only be set on
            root accounts. Child accounts are assumed to have the same time as their parent.
        TYPES (Choices): Available account types. Uses ``Choices`` from ``django-model-utils``. Types can be
            accessed in the form ``Account.TYPES.asset``, ``Account.TYPES.expense``, etc.
        has_statements (bool): Does this account have statements? (i.e. a bank account)


    """
    TYPES = Choices(
        ('AS', 'asset', 'Asset'),  # Cash in bank
        ('LI', 'liability', 'Liability'),
        ('IN', 'income', 'Income'),  # Incoming rent, contributions
        ('EX', 'expense', 'Expense'),  # Shopping, outgoing rent
        ('EQ', 'equity', 'Equity'),
    )
    uuid = SmallUUIDField(default=uuid_default(), editable=False)
    name = models.CharField(max_length=50)
    parent = TreeForeignKey('self', null=True, blank=True, related_name='children', db_index=True)
    # TODO: Denormalise account code (in order to allow lookup by it). Or add a calculated field in postgres?
    code = models.CharField(max_length=3)
    # TODO: Implement this child_code_width field, as it is probably a good idea
    # child_code_width = models.PositiveSmallIntegerField(default=1)
    _type = models.CharField(max_length=2, choices=TYPES, blank=True)
    has_statements = models.BooleanField(default=False, blank=True,
                                         help_text='Does this account have statements to reconcile against. '
                                                   'This is typically the case for bank accounts.')

    objects = AccountManager.from_queryset(AccountQuerySet)()

    class MPTTMeta:
        order_insertion_by = ['code']

    class Meta:
        unique_together = (('parent', 'code'),)

    @classmethod
    def validate_accounting_equation(cls):
        """Check that all accounts sum to 0"""
        balances = [account.balance(raw=True) for account in Account.objects.root_nodes()]
        if sum(balances) != 0:
            raise exceptions.AccountingEquationViolationError(
                'Account balances do not sum to zero. They sum to {}'.format(sum(balances))
            )

    def __str__(self):
        name = self.name or 'Unnamed Account'
        if self.is_leaf_node():
            return '{} [{}]'.format(name, self.full_code or '-')
        else:
            return name

    def natural_key(self):
        return (self.uuid,)

    @property
    def full_code(self):
        """Get the full code for this account

        Do this by concatenating this account's code with that
        of all the parent accounts.
        """
        if not self.pk:
            # Has not been saved to the DB so we cannot get ancestors
            return None
        else:
            return ''.join(account.code for account in self.get_ancestors(include_self=True))

    @property
    def type(self):
        if self.is_root_node():
            return self._type
        else:
            return self.get_root()._type

    @type.setter
    def type(self, value):
        """
        Only root nodes can have an account type. This seems like a
        sane limitation until proven otherwise.
        """
        if self.is_root_node():
            self._type = value
        else:
            raise exceptions.AccountTypeOnChildNode()

    @property
    def sign(self):
        """
        Returns 1 if a credit should increase the value of the
        account, or -1 if a credit should decrease the value of the
        account.

        This is based on the account type as is standard accounting practice.
        The signs can be derrived from the following expanded form of the
        accounting equation:

            Assets = Liabilities + Equity + (Income - Expenses)

        Which can be rearranged as:

            0 = Liabilities + Equity + Income - Expenses - Assets

        Further details here: https://en.wikipedia.org/wiki/Debits_and_credits

        """
        return -1 if self.type in (Account.TYPES.asset, Account.TYPES.expense) else 1

    def balance(self, as_of=None, raw=False, **kwargs):
        """Get the balance for this account, including child accounts

        See simple_balance() for argument reference.

        Returns:
            Decimal
        """
        balances = [
            account.simple_balance(as_of=as_of, raw=raw, **kwargs)
            for account
            in self.get_descendants(include_self=True)
        ]
        return sum(balances)

    def simple_balance(self, as_of=None, raw=False, **kwargs):
        """Get the balance for this account, ignoring all child accounts

        Args:
            as_of (Date): Only include transactions before this date
            raw (bool): If true the returned balance will not have its sign
                        adjusted for display purposes.
            **kwargs (dict): Will be used to filter the transaction legs

        Returns:
            Decimal
        """
        legs = self.legs
        if as_of:
            legs = legs.filter(transaction__date__lte=as_of)
        if kwargs:
            legs = legs.filter(**kwargs)
        return legs.sum_amount() * (1 if raw else self.sign)

    @db_transaction.atomic()
    def transfer_to(self, to_account, amount, **transaction_kwargs):
        """Create a transaction which transfers amount to to_account

        This is a shortcut utility method which simplifies the process of
        transferring between accounts.
        """
        if to_account.sign == 1:
            # Transferring from two positive-signed accounts implies that
            # the caller wants to reduce the first account and increase the second
            # (which is opposite to the implicit behaviour)
            # Question: Is this actually a good idea?
            direction = -1
        else:
            direction = 1

        transaction = Transaction.objects.create(**transaction_kwargs)
        Leg.objects.create(transaction=transaction, account=self, amount=+amount * direction)
        Leg.objects.create(transaction=transaction, account=to_account, amount=-amount * direction)
        return transaction


class TransactionManager(models.Manager):

    def get_by_natural_key(self, uuid):
        return self.get(uuid=uuid)


class Transaction(models.Model):
    """ Represents a transaction

    A transaction is a movement of funds between two accounts. Each transaction
    will have two or more legs, each leg specifies an account and an amount.

    Attributes:

        uuid (SmallUUID): UUID for transaction. Use to prevent leaking of IDs (if desired).
        timestamp (datetime): The datetime when the object was created.
        date (date): The date when the transaction actually occurred, as this may be different to
            :attr:`timestamp`.
        description (str): Optional user-provided description

    """
    uuid = SmallUUIDField(default=uuid_default(), editable=False)
    timestamp = models.DateTimeField(default=timezone.now, help_text='The creation date of this transaction object')
    date = models.DateField(default=timezone.now, help_text='The date on which this transaction occurred')
    description = models.TextField(default='', blank=True)

    objects = TransactionManager()

    def balance(self):
        return self.legs.sum_amount()

    def natural_key(self):
        return (self.uuid,)


class LegQuerySet(models.QuerySet):

    def sum_amount(self):
        return self.aggregate(models.Sum('amount'))['amount__sum'] or 0


class LegManager(models.Manager):

    def get_by_natural_key(self, uuid):
        return self.get(uuid=uuid)


class Leg(models.Model):
    """ The leg of a transaction

    Represents a single amount either into or out of a transaction. All legs for a transaction
    must sum to zero, all legs must be of the same currency.

    Attributes:

        uuid (SmallUUID): UUID for transaction leg. Use to prevent leaking of IDs (if desired).
        transaction (Transaction): Transaction to which the Leg belongs.
        account (Account): Account the leg is transferring to/from.
        amount (Money): The amount being transferred
        description (str): Optional user-provided description
        type (str): :attr:`hordak.models.DEBIT` or :attr:`hordak.models.CREDIT`.

    """
    uuid = SmallUUIDField(default=uuid_default(), editable=False)
    transaction = models.ForeignKey(Transaction, related_name='legs', on_delete=models.CASCADE)
    account = models.ForeignKey(Account, related_name='legs')
    # TODO: Assert all legs sum to zero when grouped by currency
    # TODO: Assert that the leg currency matches the account currency
    # TODO: Can accounts have multiple currencies? Should this be technically possible for all accounts but only made available for trading accounts?
    amount = MoneyField(max_digits=13, decimal_places=2,
                        help_text='Record debits as positive, credits as negative',
                        default_currency=defaults.INTERNAL_CURRENCY)
    description = models.TextField(default='', blank=True)

    objects = LegManager.from_queryset(LegQuerySet)()

    def save(self, *args, **kwargs):
        if self.amount.amount == 0:
            raise exceptions.ZeroAmountError()
        return super(Leg, self).save(*args, **kwargs)

    def natural_key(self):
        return (self.uuid,)

    @property
    def type(self):
        if self.amount.amount < 0:
            return DEBIT
        elif self.amount.amount > 0:
            return CREDIT
        else:
            # This should have been caught earlier by the database integrity check.
            # If you are seeing this then something is wrong with your DB checks.
            raise exceptions.ZeroAmountError()

    def is_debit(self):
        return self.type == DEBIT

    def is_credit(self):
        return self.type == CREDIT


class StatementImportManager(models.Manager):

    def get_by_natural_key(self, uuid):
        return self.get(uuid=uuid)


class StatementImport(models.Model):
    """ Records an import of a bank statement

    Attributes:

        uuid (SmallUUID): UUID for statement import. Use to prevent leaking of IDs (if desired).
        timestamp (datetime): The datetime when the object was created.
        bank_account (Account): The account the import is for (should normally point to an asset
            account which represents your bank account)

    """
    uuid = SmallUUIDField(default=uuid_default(), editable=False)
    timestamp = models.DateTimeField(default=timezone.now)
    # TODO: Add constraint to ensure destination account expects statements
    bank_account = models.ForeignKey(Account, related_name='imports')

    objects = StatementImportManager()

    def natural_key(self):
        return (self.uuid,)


class StatementLineManager(models.Manager):

    def get_by_natural_key(self, uuid):
        return self.get(uuid=uuid)


class StatementLine(models.Model):
    """ Records an single imported bank statement line

    A StatementLine is purely a utility to aid in the creation of transactions
    (in the process known as reconciliation). StatementLines have no impact on
    account balances.

    However, the :meth:`StatementLine.create_transaction()` method can be used to create
    a transaction based on the information in the StatementLine.

    Attributes:

        uuid (SmallUUID): UUID for statement line. Use to prevent leaking of IDs (if desired).
        timestamp (datetime): The datetime when the object was created.
        date (date): The date given by the statement line
        statement_import (StatementImport): The import to which the line belongs
        amount (Decimal): The amount for the statement line, positive or nagative.
        description (str): Any description/memo information provided
        transaction (Transaction): Optionally, the transaction created for this statement line. This normally
            occurs during reconciliation. See also :meth:`StatementLine.create_transaction()`.
    """
    uuid = SmallUUIDField(default=uuid_default(), editable=False)
    timestamp = models.DateTimeField(default=timezone.now)
    date = models.DateField()
    statement_import = models.ForeignKey(StatementImport, related_name='lines')
    amount = models.DecimalField(max_digits=13, decimal_places=2)
    description = models.TextField(default='', blank=True)
    # TODO: Add constraint to ensure transaction amount = statement line amount
    # TODO: Add constraint to ensure one statement line per transaction
    transaction = models.ForeignKey(Transaction, default=None, blank=True, null=True,
                                    help_text='Reconcile this statement line to this transaction')

    objects = StatementLineManager()

    def natural_key(self):
        return (self.uuid,)

    @property
    def is_reconciled(self):
        """Has this statement line been reconciled?

        Determined as ``True`` if :attr:`transaction` has been set.

        Returns:
            bool: ``True`` if reconciled, ``False`` if not.
        """
        return bool(self.transaction)

    @db_transaction.atomic()
    def create_transaction(self, to_account):
        """Create a transaction for this statement amount and account, into to_account

        This will also set this StatementLine's ``transaction`` attribute to the newly
        created transaction.

        Args:
            to_account (Account): The account the transaction is into / out of.

        Returns:
            Transaction: The newly created (and committed) transaction.

        """
        from_account = self.statement_import.bank_account

        transaction = Transaction.objects.create()
        Leg.objects.create(transaction=transaction, account=from_account, amount=+(self.amount * -1))
        Leg.objects.create(transaction=transaction, account=to_account, amount=-(self.amount * -1))

        transaction.date = self.date
        transaction.save()

        self.transaction = transaction
        self.save()
        return transaction
