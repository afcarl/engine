import functools
from py4j.java_gateway import java_import
from pyspark.sql import DataFrame
from bblfsh.sdkversion import VERSION


class Engine(object):
    """
    This is the entry point to any functionality exposed by the source{d}
    Engine. It contains methods to initialize the analysis on top of source
    code repositories.

    >>> from sourced.engine import Engine
    >>> repos_df = Engine(sparkSession, "/path/to/my/repositories").repositories
    >>> repos_df.show()

    :param session: spark session to use
    :type session: pyspark.sql.SparkSession
    :param repos_path: path to the folder where siva files are stored
    :type repos_path: str
    :param repos_format: format of the repositories inside the provided folder.
    It can be siva, bare or standard.
    :type repos_format: str
    :param skip_cleanup: don't delete unpacked siva files after using them
    :type skip_cleanup: bool
    :param skip_read_errors: skip any error encountered during repository reads
    :type skip_read_errors: bool
    """

    def __init__(self, session, repos_path, repos_format, skip_cleanup=False, skip_read_errors=False):
        self.session = session
        self.__jsparkSession = session._jsparkSession
        self.session.conf.set('spark.tech.sourced.engine.repositories.path', repos_path)
        self.session.conf.set('spark.tech.sourced.engine.repositories.format', repos_format)
        self.__jvm = self.session.sparkContext._gateway.jvm
        java_import(self.__jvm, 'tech.sourced.engine.Engine')
        java_import(self.__jvm, 'tech.sourced.engine.package$')

        try:
            self.__engine = self.__jvm.tech.sourced.engine.Engine.apply(self.__jsparkSession, repos_path, repos_format)
        except TypeError as e:
            if 'JavaPackage' in e.args[0]:
                raise Exception("package \"tech.sourced:engine:<version>\" cannot be found. Please, provide a jar with the package or install the package using --packages")
            else:
                raise e

        if skip_cleanup:
            self.__engine.skipCleanup(True)

        if skip_read_errors:
            self.__engine.skipReadErrors(True)

        self.__implicits = getattr(getattr(self.__jvm.tech.sourced.engine, 'package$'), 'MODULE$')


    @property
    def repositories(self):
        """
        Returns a DataFrame with the data about the repositories found at
        the specified repositories path in the form of siva files.

        >>> repos_df = engine.repositories

        :rtype: RepositoriesDataFrame
        """
        return RepositoriesDataFrame(self.__engine.getRepositories(),
                                     self.session, self.__implicits)


    def blobs(self, repository_ids=[], reference_names=[], commit_hashes=[]):
        """
        Retrieves the blobs of a list of repositories, reference names and commit hashes.
        So the result will be a DataFrame of all the blobs in the given commits that are
        in the given references that belong to the given repositories.

        >>> blobs_df = engine.blobs(repo_ids, ref_names, hashes)

        Calling this function with no arguments is the same as:

        >>> engine.repositories.references.commits.tree_entries.blobs

        :param repository_ids: list of repository ids to filter by (optional)
        :type repository_ids: list of strings
        :param reference_names: list of reference names to filter by (optional)
        :type reference_names: list of strings
        :param commit_hashes: list of hashes to filter by (optional)
        :type commit_hashes: list of strings
        :rtype: BlobsDataFrame
        """
        if not isinstance(repository_ids, list):
            raise Exception("repository_ids must be a list")

        if not isinstance(reference_names, list):
            raise Exception("reference_names must be a list")

        if not isinstance(commit_hashes, list):
            raise Exception("commit_hashes must be a list")

        return BlobsDataFrame(self.__engine.getBlobs(repository_ids,
                                                  reference_names,
                                                  commit_hashes),
                              self.session,
                              self.__implicits)


    def from_metadata(self, db_path, db_name='engine_metadata.db'):
        """
        Registers in the current session the views of the MetadataSource so the
        data is obtained from the metadata database instead of reading the
        repositories with the DefaultSource.

        :param db_path: path to the folder that contains the database.
        :type db_path: str
        :param db_name: name of the database file (engine_metadata.db) by default.
        :type db_name: str
        :returns: the same instance of the engine
        :rtype: Engine
        """

        self.__engine.fromMetadata(db_path, db_name)
        return self

    def from_repositories(self):
        """
        Registers in the current session the views of the DefaultSource so the
        data is obtained by reading the repositories instead of reading from
        the MetadataSource. This has no effect if :method:`fromMetadata` has
        not been called before.

        :returns: the same instance of the engine
        :rtype: Engine
        """

        return self.__engine.fromRepositories()

    def save_metadata(self, path, db_name='engine_metadata.db'):
        """
        Saves all the metadata in a SQLite database on the given path with the given filename
        (which if not given is "engine_metadata.db". If the database already exists, it will be
        overwritten. The given path must exist and must be a directory, otherwise it will throw
        a [[SparkException]].
        Saved tables are repositories, references, commits and tree_entries. Blobs are not saved.

        :param path:   where database with the metadata will be stored.
        :param db_name: name of the database file (default is "engine_metadata.db")
        :raise Exception: when the given path is not a folder or does not exist.
        """

        self.__engine.saveMetadata(path, db_name)


class SourcedDataFrame(DataFrame):
    """
    Custom source{d} Engine DataFrame that contains some DataFrame overriden methods and
    utilities. This class should not be used directly, please get your SourcedDataFrames
    using the provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        DataFrame.__init__(self, jdf, session)
        self._session = session
        self._implicits = implicits


    @property
    def _engine_dataframe(self):
        return self._implicits.EngineDataFrame(self._jdf)

    def __generate_method(name):
        """
        Wraps the DataFrame's original method by name to return the derived class instance.
        """
        try:
            func = getattr(DataFrame, name)
        except AttributeError as e:
            # PySpark version is too old
            def func(self, *args, **kwargs):
                raise e
            return func
        wraps = getattr(functools, "wraps", lambda _: lambda f: f)  # py3.4+

        @wraps(func)
        def _wrapper(self, *args, **kwargs):
            dataframe = func(self, *args, **kwargs)
            if self.__class__ != SourcedDataFrame \
                    and isinstance(self, SourcedDataFrame) \
                    and isinstance(dataframe, DataFrame):
                return self.__class__(dataframe._jdf, self._session, self._implicits)
            return dataframe

        return _wrapper

    # The following code wraps all the methods of DataFrame as of 2.3
    alias = __generate_method("alias")
    checkpoint = __generate_method("checkpoint")
    coalesce = __generate_method("coalesce")
    crossJoin = __generate_method("crossJoin")
    crosstab = __generate_method("crosstab")
    describe = __generate_method("describe")
    distinct = __generate_method("distinct")
    dropDuplicates = __generate_method("dropDuplicates")
    drop_duplicates = dropDuplicates
    drop = __generate_method("drop")
    dropna = __generate_method("dropna")
    fillna = __generate_method("fillna")
    filter = __generate_method("filter")
    freqItems = __generate_method("freqItems")
    hint = __generate_method("hint")
    intersect = __generate_method("intersect")
    join = __generate_method("join")
    limit = __generate_method("limit")
    randomSplit = __generate_method("randomSplit")
    repartition = __generate_method("repartition")
    replace = __generate_method("replace")
    sampleBy = __generate_method("sampleBy")
    sample = __generate_method("sample")
    selectExpr = __generate_method("selectExpr")
    select = __generate_method("select")
    sort = __generate_method("sort")
    orderBy = sort
    sortWithinPartitions = __generate_method("sortWithinPartitions")
    subtract = __generate_method("subtract")
    summary = __generate_method("summary")
    toDF = __generate_method("toDF")
    unionByName = __generate_method("unionByName")
    union = __generate_method("union")
    where = filter
    withColumn = __generate_method("withColumn")
    withColumnRenamed = __generate_method("withColumnRenamed")
    withWatermark = __generate_method("withWatermark")

    __generate_method = staticmethod(__generate_method)  # to make IntelliSense happy


class RepositoriesDataFrame(SourcedDataFrame):
    """
    DataFrame containing repositories.
    This class should not be instantiated directly, please get your RepositoriesDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    @property
    def references(self):
        """
        Returns the joined DataFrame of references and repositories.

        >>> refs_df = repos_df.references

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getReferences(),
                                   self._session, self._implicits)


    @property
    def remote_references(self):
        """
        Returns a new DataFrame with only the remote references of the
        current repositories.

        >>> remote_refs_df = repos_df.remote_references

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getRemoteReferences(),
                                   self._session, self._implicits)


    @property
    def head_ref(self):
        """
        Filters the current DataFrame references to only contain those rows whose reference is HEAD.

        >>> heads_df = repos_df.head_ref

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getReferences().getHEAD(),
                                   self._session, self._implicits)


    @property
    def master_ref(self):
        """
        Filters the current DataFrame references to only contain those rows whose reference is master.

        >>> master_df = repos_df.master_ref

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getReferences().getHEAD(),
                                   self._session, self._implicits)


class ReferencesDataFrame(SourcedDataFrame):
    """
    DataFrame with references.
    This class should not be instantiated directly, please get your ReferencesDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    @property
    def remote_references(self):
        """
        Returns a new DataFrame with only the remote references of all the current
        references.

        >>> remote_refs_df = refs_df.remote_references

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getRemoteReferences(),
                                   self._session, self._implicits)


    @property
    def head_ref(self):
        """
        Filters the current DataFrame to only contain those rows whose reference is HEAD.

        >>> heads_df = refs_df.head_ref

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getHEAD(),
                                   self._session, self._implicits)


    @property
    def master_ref(self):
        """
        Filters the current DataFrame to only contain those rows whose reference is master.

        >>> master_df = refs_df.master_ref

        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self._engine_dataframe.getMaster(),
                                   self._session, self._implicits)
        return self.ref('refs/heads/master')


    def ref(self, ref):
        """
        Filters the current DataFrame to only contain those rows whose reference is the given
        reference name.

        >>> heads_df = refs_df.ref('refs/heads/HEAD')

        :param ref: Reference to get
        :type ref: str
        :rtype: ReferencesDataFrame
        """
        return ReferencesDataFrame(self.filter(self.name == ref)._jdf,
                                   self._session, self._implicits)


    @property
    def all_reference_commits(self):
        """
        Returns the current DataFrame joined with the commits DataFrame, with all of the commits
        in all references.

        >>> commits_df = refs_df.all_reference_commits

        Take into account that getting all the commits will lead to a lot of repeated tree
        entries and blobs, thus making your query very slow.

        Most of the time, you just want the HEAD commit of each reference:

        >>> commits_df = refs_df.commits

        :rtype: CommitsDataFrame
        """
        return CommitsDataFrame(self._engine_dataframe.getAllReferenceCommits(), self._session, self._implicits)


    @property
    def commits(self):
        """
        Returns the current DataFrame joined with the commits DataFrame. It just returns
        the last commit in a reference (aka the current state).

        >>> commits_df = refs_df.commits

        If you want all commits from the references, use the `all_reference_commits` method,
        but take into account that getting all the commits will lead to a lot of repeated tree
        entries and blobs, thus making your query very slow.

        >>> commits_df = refs_df.all_reference_commits

        :rtype: CommitsDataFrame
        """
        return CommitsDataFrame(self._engine_dataframe.getCommits(), self._session, self._implicits)


    @property
    def blobs(self):
        """
        Returns this DataFrame joined with the blobs DataSource.

        >>> blobs_df = refs_df.blobs

        :rtype: BlobsDataFrame
        """
        return BlobsDataFrame(self._engine_dataframe.getBlobs(), self._session, self._implicits)


class CommitsDataFrame(SourcedDataFrame):
    """
    DataFrame with commits data.
    This class should not be instantiated directly, please get your CommitsDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    @property
    def all_reference_commits(self):
        """
        Returns all the commits in all references.

        >>> all_commits_df = commits_df.all_reference_commits

        Take into account that getting all the commits will lead to a lot of repeated tree
        entries and blobs, thus making your query very slow.

        Most of the time, you just want the HEAD commit of each reference:

        >>> commits_df = refs_df.commits

        :rtype: CommitsDataFrame
        """
        return CommitsDataFrame(self._engine_dataframe.getAllReferenceCommits(), self._session, self._implicits)


    @property
    def tree_entries(self):
        """
        Returns this DataFrame joined with the tree entries DataSource.

        >>> entries_df = commits_df.tree_entries

        :rtype: TreeEntriesDataFrame
        """
        return TreeEntriesDataFrame(self._engine_dataframe.getTreeEntries(), self._session, self._implicits)


    @property
    def blobs(self):
        """
        Returns a new DataFrame with the blob associated to each tree entry of the commit.

        >>> > blobs_df = commits_df.blobs

        :rtype: BlobsDataFrame
        """
        return BlobsDataFrame(self._engine_dataframe.getBlobs(), self._session,
                                self._implicits)


class TreeEntriesDataFrame(SourcedDataFrame):
    """
    DataFrame with tree entries data data.
    This class should not be instantiated directly, please get your TreeEntriesDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    @property
    def blobs(self):
        """
        Returns a new DataFrame with the blob associated to each tree entry.

        >>> > blobs_df = trees_df.blobs

        :rtype: BlobsDataFrame
        """
        return BlobsDataFrame(self._engine_dataframe.getBlobs(), self._session,
                              self._implicits)


class BlobsDataFrame(SourcedDataFrame):
    """
    DataFrame containing blobs data.
    This class should not be instantiated directly, please get your BlobsDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    def classify_languages(self):
        """
        Returns a new DataFrame with the language data of any blob added to
        its row.

        >>> blobs_lang_df = blobs_df.classify_languages

        :rtype: BlobsWithLanguageDataFrame
        """
        return BlobsWithLanguageDataFrame(self._engine_dataframe.classifyLanguages(),
                                          self._session, self._implicits)


    def extract_uasts(self):
        """
        Returns a new DataFrame with the parsed UAST data of any blob added to
        its row.

        >>> blobs_df.extract_uasts

        :rtype: UASTsDataFrame
        """
        return UASTsDataFrame(self._engine_dataframe.extractUASTs(),
                              self._session, self._implicits)


class BlobsWithLanguageDataFrame(SourcedDataFrame):
    """
    DataFrame containing blobs and language data.
    This class should not be instantiated directly, please get your BlobsWithLanguageDataFrame
    using the provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    def extract_uasts(self):
        """
        Returns a new DataFrame with the parsed UAST data of any blob added to
        its row.

        >>> blobs_lang_df.extract_uasts

        :rtype: UASTsDataFrame
        """
        return UASTsDataFrame(self._engine_dataframe.extractUASTs(),
                              self._session, self._implicits)


class UASTsDataFrame(SourcedDataFrame):
    """
    DataFrame containing UAST data.
    This class should not be instantiated directly, please get your UASTsDataFrame using the
    provided methods.

    :param jdf: Java DataFrame
    :type jdf: py4j.java_gateway.JavaObject
    :param session: Spark Session to use
    :type session: pyspark.sql.SparkSession
    :param implicits: Implicits object from Scala
    :type implicits: py4j.java_gateway.JavaObject
    """

    def __init__(self, jdf, session, implicits):
        SourcedDataFrame.__init__(self, jdf, session, implicits)


    def query_uast(self, query, query_col='uast', output_col='result'):
        """
        Queries the UAST of a file with the given query to get specific nodes.

        >>> rows = uasts_df.query_uast('//*[@roleIdentifier]').collect()
        >>> rows = uasts_df.query_uast('//*[@roleIdentifier]', 'foo', 'bar')

        :param query: xpath query
        :type query: str
        :param query_col: column containing the list of nodes to query
        :type query_col: str
        :param output_col: column to place the result of the query
        :type output_col: str
        :rtype: UASTsDataFrame
        """
        return UASTsDataFrame(self._engine_dataframe.queryUAST(query,
                                       query_col,
                                       output_col),
                              self._session, self._implicits)


    def extract_tokens(self, input_col='result', output_col='tokens'):
        """
        Extracts the tokens from UAST nodes.

        >>> rows = uasts_df.query_uast('//*[@roleIdentifier]').extract_tokens().collect()
        >>> rows = uasts_df.query_uast('//*[@roleIdentifier]', output_col='foo').extract_tokens('foo', 'bar')

        :param input_col: column containing the list of nodes to extract tokens from
        :type input_col: str
        :param output_col: column to place the resultant tokens
        :type output_col: str
        :rtype: UASTsDataFrame
        """
        return UASTsDataFrame(self._engine_dataframe.extractTokens(input_col, output_col),
                              self._session, self._implicits)
