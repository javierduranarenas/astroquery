import time
import os.path
import webbrowser
import getpass
from cStringIO import StringIO

from astropy.extern import six
from astropy.table import Table, Column
from astropy.io import ascii
from bs4 import BeautifulSoup

from ..utils import schema, system_tools
from ..query import QueryWithLogin, suspend_cache
from . import ROW_LIMIT


class EsoClass(QueryWithLogin):

    ROW_LIMIT = ROW_LIMIT()

    def __init__(self):
        super(EsoClass, self).__init__()
        self._instrument_list = None
        self._survey_list = None

    def _activate_form(self, response, form_index=0, inputs={}):
        #Extract form from response
        root = BeautifulSoup(response.content)
        form = root.find_all('form')[form_index]
        #Construct base url
        form_action = form.get('action')
        if "://" in form_action:
            url = form_action
        elif form_action.startswith("/"):
            url = '/'.join(response.url.split('/',3)[:3]) + form_action
        else:
            url = response.url.rsplit('/',1)[0] + '/' + form_action
        #Identify payload format
        if form.get('method') == 'get':
            fmt = 'get' #get(url, params=payload)
        elif form.get('method') == 'post':
            if 'enctype' in form.attrs:
                if form.attrs['enctype'] == 'multipart/form-data':
                    fmt = 'multipart/form-data' #post(url, files=payload)
                elif form.attrs['enctype'] == 'application/x-www-form-urlencoded':
                    fmt = 'application/x-www-form-urlencoded' #post(url, data=payload)
            else:
                fmt = 'post'  # post(url, params=payload)
        # Extract payload from form
        payload = []
        for form_elem in form.find_all(['input', 'select', 'textarea']):
            value = None
            is_file = False
            tag_name = form_elem.name
            if tag_name == 'input':
                value = form_elem.get('value')
                if 'type' in form_elem.attrs:
                    is_file = form_elem.get('type') == 'file'
            elif tag_name == 'select':
                if form_elem.get('multiple') is not None:
                    value = []
                    for option in form_elem.select('option[value]'):
                        if option.get('selected') is not None:
                            value.append(option.get('value'))
                else:
                    for option in form_elem.select('option[value]'):
                        if option.get('selected') is not None:
                            value = option.get('value')
            if tag_name in inputs:
                value = str(inputs[tag_name])
            if value is not None:
                if fmt == 'multipart/form-data':
                    if is_file:
                        payload += [(tag_name, ('', '', 'application/octet-stream'))]
                    else:
                        if type(value) is list:
                            for v in value:
                                payload += [(tag_name, ('', v))]
                        else:
                            payload += [(tag_name, ('', value))]
                else:
                    if type(value) is list:
                        for v in value:
                            payload += [(tag_name, v)]
                    else:
                        payload += [(tag_name, value)]

        #Send payload
        if fmt == 'get':
            response = self.request("GET", url, params=payload)
        elif fmt == 'post':
            response = self.request("POST", url, params=payload)
        elif fmt == 'multipart/form-data':
            response = self.request("POST", url, files=payload)
        elif fmt == 'application/x-www-form-urlencoded':
            response = self.request("POST", url, data=payload)
        return response

    def _login(self, username):
        import keyring
        from lxml import html
        # Get password from keyring or prompt
        password_from_keyring = keyring.get_password("astroquery:www.eso.org", username)
        if password_from_keyring is None:
            password = getpass.getpass("{0}, enter your ESO password:\n".format(username))
        else:
            password = password_from_keyring
        #Authenticate
        print("Authenticating {} on www.eso.org...".format(username))
        login_response = self.session.get("https://www.eso.org/sso/login")
        login_result_response = self._activate_form(login_response, form_index=-1, inputs={'username': username, 'password':password})
        root = BeautifulSoup(login_result_response.content)
        authenticated = not root.select('.error')
        if authenticated:
            print("Authentication successful!")
        else:
            print("Authentication failed!")
        # When authenticated, save password in keyring if needed
        if authenticated and password_from_keyring is None:
            keyring.set_password("astroquery:www.eso.org", username, password)
        return authenticated

    def list_instruments(self):
        """ List all the available instruments in the ESO archive.

        Returns
        -------
        instrument_list : list of strings

        """
        from lxml import html
        if self._instrument_list is None:
            instrument_list_response = self.session.get("http://archive.eso.org/cms/eso-data/instrument-specific-query-forms.html")
            root = BeautifulSoup(instrument_list_response.content)
            self._instrument_list = []
            for element in root.select('div[id="col3"] a'):
                href = element.get("href", "")
                if "http://archive.eso.org/wdb/wdb/eso" in href:
                    instrument = href.split("/")[-2]
                    if instrument not in self._instrument_list:
                        self._instrument_list.append(instrument)
        return self._instrument_list

    def list_surveys(self):
        """ List all the available surveys (phase 3) in the ESO archive.

        Returns
        -------
        survey_list : list of strings

        """
        from lxml import html
        if self._survey_list is None:
            survey_list_response = self.session.get("http://archive.eso.org/wdb/wdb/adp/phase3_main/form")
            root = BeautifulSoup(survey_list_response.content)
            self._survey_list = []
            for select in root.find_all('select', {'name': 'phase3_program'}):
                for element in select.find_all('option'):
                    survey = ''.join(element.stripped_strings)
                    if survey not in self._survey_list and 'Any' not in survey:
                        self._survey_list.append(survey)
        return self._survey_list

    def query_survey(self, survey, **kwargs):
        """
        Query survey Phase 3 data contained in the ESO archive.

        Parameters
        ----------
        survey : string
            Name of the survey to query, one of the names returned by
            `list_surveys()`.

        Returns
        -------
        table : `~astropy.table.Table` or `None`
            A table representing the data available in the archive for the
            specified survey, matching the constraints specified in ``kwargs``.
            The number of rows returned is capped by the ROW_LIMIT
            configuration item. `None` is returned when the query has no
            results.

        """

        if survey not in self.list_surveys():
            raise ValueError("Survey %s is not in the survey list." % survey)
        url = "http://archive.eso.org/wdb/wdb/adp/phase3_main/form"
        survey_form = self.request("GET", url)
        query_dict = kwargs
        query_dict["wdbo"] = "csv/download"
        query_dict['phase3_program'] = survey
        if self.ROW_LIMIT >= 0:
            query_dict["max_rows_returned"] = self.ROW_LIMIT
        else:
            query_dict["max_rows_returned"] = 10000
        survey_response = self._activate_form(survey_form, form_index=0,
                                              inputs=query_dict)

        if b"# No data returned !" not in survey_response.content:
            table = ascii.read(StringIO(survey_response.content.decode(
                               survey_response.encoding)), format='csv',
                               comment='#', delimiter=',', header_start=1)
            return table
        else:
            warnings.warn("Query returned no results")



    def query_instrument(self, instrument, open_form=False, **kwargs):
        """
        Query instrument specific raw data contained in the ESO archive.

        Parameters
        ----------
        instrument : string
            Name of the instrument to query, one of the names returned by
            `list_instruments()`.
        open_form : bool
            If `True`, this will open in your browser the query form
            for the given instrument, and return `None`.

        Returns
        -------
        table : `~astropy.table.Table`
            A table representing the data available in the archive for the
            specified instrument, matching the constraints specified in
            ``kwargs``. The number of rows returned is capped by the
            ROW_LIMIT configuration item.

        """

        url = "http://archive.eso.org/wdb/wdb/eso/{0}/form".format(instrument)
        table = None
        if open_form:
            webbrowser.open(url)
        else:
            instrument_form = self.request("GET", url)
            query_dict = kwargs
            query_dict["wdbo"] = "csv/download"
            if self.ROW_LIMIT >= 0:
                query_dict["max_rows_returned"] = self.ROW_LIMIT
            else:
                query_dict["max_rows_returned"] = 10000
            instrument_response = self._activate_form(instrument_form,
                                                      form_index=0,
                                                      inputs=query_dict)
            if b"# No data returned !" not in instrument_response.content:
                content = []
                # The first line is garbage, don't know why
                for line in instrument_response.content.split(b'\n')[1:]:
                    if len(line) > 0:  # Drop empty lines
                        if line[0:1] != b'#':  # And drop comments
                            content += [line]
                        else:
                            warnings.warn("Query returned no results")
                content = b'\n'.join(content)
                table = Table.read(BytesIO(content), format="ascii.csv")
        return table

    def get_headers(self, product_ids):
        """
        Get the headers associated to a list of data product IDs

        This method returns a `~astropy.table.Table` where the rows correspond
        to the provided data product IDs, and the columns are from each of
        the Fits headers keywords.

        Note: The additional column ``'DP.ID'`` found in the returned table
        corresponds to the provided data product IDs.

        Parameters
        ----------
        product_ids : either a list of strings or a `~astropy.table.Column`
            List of data product IDs.

        Returns
        -------
        result : `~astropy.table.Table`
            A table where: columns are header keywords, rows are product_ids.

        """
        from lxml import html
        _schema_product_ids = schema.Schema(schema.Or(Column, [six.string_types]))
        _schema_product_ids.validate(product_ids)
        # Get all headers
        result = []
        for dp_id in product_ids:
            response = self.request("GET", "http://archive.eso.org/hdr?DpId={0}".format(dp_id))
            root = html.document_fromstring(response.content)
            hdr = root.xpath("//pre")[0].text
            header = {'DP.ID': dp_id}
            for key_value in hdr.split('\n'):
                if "=" in key_value:
                    [key, value] = key_value.split('=', 1)
                    key = key.strip()
                    value = value.split('/', 1)[0].strip()
                    if key[0:7] != "COMMENT":  # drop comments
                        if value == "T":  # Convert boolean T to True
                            value = True
                        elif value == "F":  # Convert boolean F to False
                            value = False
                        # Convert to string, removing quotation marks
                        elif value[0] == "'":
                            value = value[1:-1]
                        elif "." in value:  # Convert to float
                                value = float(value)
                        else:  # Convert to integer
                            value = int(value)
                        header[key] = value
                elif key_value.find("END") == 0:
                    break
            result += [header]
        # Identify all columns
        columns = []
        column_types = []
        for header in result:
            for key in header.keys():
                if key not in columns:
                    columns += [key]
                    column_types += [type(header[key])]
        # Add all missing elements
        for i in range(len(result)):
            for (column, column_type) in zip(columns, column_types):
                if column not in result[i]:
                    result[i][column] = column_type()
        # Return as Table
        return Table(result)

    def data_retrieval(self, datasets):
        """
        Retrieve a list of datasets form the ESO archive.

        Parameters
        ----------
        datasets : list of strings
            List of datasets strings to retrieve from the archive.

        Returns
        -------
        files : list of strings
            List of files that have been locally downloaded from the archive.

        """
        data_retrieval_form = self.session.get("http://archive.eso.org/cms/eso-data/eso-data-direct-retrieval.html")
        data_confirmation_form = self._activate_form(data_retrieval_form, form_index=-1, inputs={"list_of_datasets": "\n".join(datasets)})
        data_download_form = self._activate_form(data_confirmation_form, form_index=-1)
        root = BeautifulSoup(data_download_form.content)
        state = root.select('span[id="requestState"]')[0].text
        while state != u'COMPLETE':
            time.sleep(2.0)
            data_download_form = self.session.get(data_download_form.url)
            root = BeautifulSoup(data_download_form.content)
            state = root.select('span[id="requestState"]')[0].text
        files = []
        for fileId in root.select('input[name="fileId"]'):
            fileLink = fileId.attrs['value'].split()[1]
            fileLink = fileLink.replace('/api', '').replace('https://', 'http://')
            files.append(self._download_file(fileLink))

        # First: Detect datasets already downloaded
        for dataset in datasets:
            local_filename = dataset + ".fits"
            if self.cache_location is not None:
                local_filename = os.path.join(self.cache_location,
                                              local_filename)
            if os.path.exists(local_filename):
                print("Found {0}.fits...".format(dataset))
                files += [local_filename]
            elif os.path.exists(local_filename + ".Z"):
                print("Found {0}.fits.Z...".format(dataset))
                files += [local_filename + ".Z"]
            else:
                datasets_to_download += [dataset]
        # Second: Download the other datasets
        if datasets_to_download:
            data_retrieval_form = self.request("GET", "http://archive.eso.org/cms/eso-data/eso-data-direct-retrieval.html")
            print("Staging request...")
            with suspend_cache(self):  # Never cache staging operations
                data_confirmation_form = self._activate_form(data_retrieval_form, form_index=-1, inputs={"list_of_datasets": "\n".join(datasets_to_download)})
                data_download_form = self._activate_form(data_confirmation_form, form_index=-1)
                root = html.document_fromstring(data_download_form.content)
                state = root.xpath("//span[@id='requestState']")[0].text
                while state != 'COMPLETE':
                    time.sleep(2.0)
                    data_download_form = self.request("GET",
                                                      data_download_form.url)
                    root = html.document_fromstring(data_download_form.content)
                    state = root.xpath("//span[@id='requestState']")[0].text
            print("Downloading files...")
            for fileId in root.xpath("//input[@name='fileId']"):
                fileLink = fileId.attrib['value'].split()[1]
                fileLink = fileLink.replace("/api", "").replace("https://", "http://")
                filename = self.request("GET", fileLink, save=True)
                files += [system_tools.gunzip(filename)]
        print("Done!")
        return files


Eso = EsoClass()
